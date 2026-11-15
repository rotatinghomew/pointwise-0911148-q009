"""分层锚定的类别共识与供应商可靠度分析。

设计约束：
* 供应商可靠度只用 expert_versions 作为锚点估计，绝不用多数票反过来给供应商打分。
* 没有专家锚点、也没有交叠的连通分组不排名，其建议标记为未经校验。
* 每条建议都带复核来源、专家版本、派单记录和不确定性，便于对外追溯。
* 结果不覆盖历史请求；新复核用新 request_id 重算并通过 supersedes 记录版本链。
"""

import hashlib
import math
import random
from collections import Counter, defaultdict
from itertools import permutations

from app.contracts import latest

METHOD = 'anchored_stratified_consensus'
METHOD_VERSION = '1.0'
ABSTAIN = 'abstain'

DEFAULT_MIN_ANCHORS = 5
DEFAULT_EXPERT_SLOTS = 12
DEFAULT_CONSENSUS_THRESHOLD = 0.8
DEFAULT_LOWER_CONFIDENCE_FLOOR = 0.65
PRIOR_STRENGTH = 2.0
MC_DRAWS = 300
CALIBRATION_EDGES = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0000001]
KNOWN_OPTIONS = {'min_anchors', 'expert_slots', 'consensus_threshold', 'lower_confidence_floor'}


def _round(value, digits=6):
    return None if value is None else round(float(value), digits)


def _fraction(correct, total):
    return None if total <= 0 else round(correct / total, 6)


def wilson(correct, total, z=1.96):
    if total <= 0:
        return None
    p = correct / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [_round(max(0.0, centre - half)), _round(min(1.0, centre + half))]


def _beta_params(records, prior_mean, prior_strength=PRIOR_STRENGTH):
    correct = sum(1 for record in records if record['correct'])
    wrong = len(records) - correct
    return (prior_mean * prior_strength + correct,
            (1 - prior_mean) * prior_strength + wrong)


def _smoothed(records, prior_mean):
    a, b = _beta_params(records, prior_mean)
    return _round(a / (a + b))


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return _round(ordered[lower])
    weight = position - lower
    return _round(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _seed(*parts):
    text = '|'.join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)


def _natural_key(value):
    parts = []
    digits = ''
    for character in str(value):
        if character.isdigit():
            digits += character
        else:
            if digits:
                parts.append((1, int(digits)))
                digits = ''
            parts.append((0, character))
    if digits:
        parts.append((1, int(digits)))
    return tuple(parts)


def _issue(issues, table, item_id, reason):
    issues.append({'table': table, 'item_id': item_id, 'reason': reason})


def _chance(categories):
    return 1.0 / len(categories) if categories else 0.0


def _context_of(assignment, degree):
    if not assignment:
        return {'degree': degree, 'probability': 'unknown'}
    probabilities = sorted({row.get('probability') for row in assignment
                           if isinstance(row.get('probability'), (int, float))})
    if len(probabilities) == 1:
        probability = probabilities[0]
    elif probabilities:
        probability = 'mixed'
    else:
        probability = 'unknown'
    return {'degree': degree, 'probability': probability}


def _context_key(context):
    return f"{context['degree']}|{context['probability']}"


def _prepare(snapshot):
    """整理可见快照，返回去重后的复核、专家版本和连通分组。"""
    issues = []
    items = {}
    for row in snapshot.get('items', []):
        item_id = row.get('item_id')
        if not item_id:
            _issue(issues, 'items', item_id, '缺少 item_id')
            continue
        categories = row.get('categories')
        if not isinstance(categories, list) or not categories:
            _issue(issues, 'items', item_id, 'categories 须为非空类别集合')
            categories = []
        if item_id in items:
            _issue(issues, 'items', item_id, '重复 item_id，保留首条')
            continue
        items[item_id] = {
            'item_id': item_id,
            'stratum': row.get('stratum', 'unknown'),
            'categories': list(dict.fromkeys(categories)),
        }

    assignments_by_item = defaultdict(list)
    assignments_by_pair = defaultdict(list)
    for row in snapshot.get('assignments', []):
        item_id, supplier = row.get('item_id'), row.get('supplier')
        if item_id not in items:
            _issue(issues, 'assignments', item_id, '引用未知资料，已忽略')
            continue
        if not supplier:
            _issue(issues, 'assignments', item_id, '缺少 supplier，已忽略')
            continue
        assignments_by_item[item_id].append(row)
        assignments_by_pair[(item_id, supplier)].append(row)

    reviews = []
    for row in snapshot.get('reviews', []):
        item_id, supplier, round_number = row.get('item_id'), row.get('supplier'), row.get('round')
        if item_id not in items:
            _issue(issues, 'reviews', item_id, '引用未知资料，已忽略')
            continue
        if not supplier or not isinstance(round_number, int):
            _issue(issues, 'reviews', item_id, '缺少 supplier 或整数 round，已忽略')
            continue
        label = row.get('label')
        valid = label == ABSTAIN or label in items[item_id]['categories']
        if not valid:
            _issue(issues, 'reviews', item_id, f'类别 {label!r} 不在合法集合内')
        reviews.append({**row, '_valid': valid})

    rounds_by_pair = defaultdict(set)
    for row in reviews:
        rounds_by_pair[(row['item_id'], row['supplier'])].add(row['round'])
    current_by_pair = {}
    for row in latest(reviews, ['item_id', 'supplier', 'round']):
        pair = (row['item_id'], row['supplier'])
        previous = current_by_pair.get(pair)
        if previous is None or row['round'] > previous['round']:
            current_by_pair[pair] = row
    current_by_item = defaultdict(list)
    for (item_id, _), row in current_by_pair.items():
        current_by_item[item_id].append(row)
    for rows in current_by_item.values():
        rows.sort(key=lambda row: row['supplier'])

    expert_history = defaultdict(list)
    for row in snapshot.get('expert_versions', []):
        item_id = row.get('item_id')
        if item_id not in items:
            _issue(issues, 'expert_versions', item_id, '引用未知资料，已忽略')
            continue
        label = row.get('expert_label')
        valid = label == ABSTAIN or label in items[item_id]['categories']
        if not valid:
            _issue(issues, 'expert_versions', item_id, f'专家类别 {label!r} 不在合法集合内')
        expert_history[item_id].append({**row, '_valid': valid})
    for rows in expert_history.values():
        rows.sort(key=lambda row: (row.get('revision', 0), row.get('received_at', '')))
    expert_current = {}
    for row in latest(snapshot.get('expert_versions', []), ['item_id']):
        if row.get('item_id') in items:
            expert_current[row['item_id']] = {
                **row,
                '_valid': row.get('expert_label') == ABSTAIN
                or row.get('expert_label') in items[row['item_id']]['categories'],
            }

    # 供应商-资料证据图的连通分组。排名不得跨组比较。
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    # 派单也构成证据关系：只派单未复核的资料仍属于某个分组，
    # 但该分组若没有锚点就不会被排名。
    for item_id, rows in assignments_by_item.items():
        for row in rows:
            union('i:' + item_id, 's:' + row['supplier'])
    for item_id, rows in current_by_item.items():
        for row in rows:
            union('i:' + item_id, 's:' + row['supplier'])

    groups = defaultdict(lambda: {'items': set(), 'suppliers': set()})
    roots = {}
    for item_id in items:
        root = find('i:' + item_id)
        roots[item_id] = root
        groups[root]['items'].add(item_id)
    for item_id, rows in assignments_by_item.items():
        groups[roots[item_id]]['suppliers'].update(row['supplier'] for row in rows)
    for item_id, rows in current_by_item.items():
        groups[roots[item_id]]['suppliers'].update(row['supplier'] for row in rows)
    component_of_item = {}
    component_of_supplier = {}
    component_ids = {}
    for root, group in groups.items():
        component_id = min(group['items'], key=_natural_key)
        component_ids[root] = component_id
        for item_id in group['items']:
            component_of_item[item_id] = component_id
        for supplier in group['suppliers']:
            component_of_supplier[supplier] = component_id
    all_suppliers = {row['supplier'] for row in snapshot.get('assignments', []) if row.get('supplier')}
    all_suppliers |= {row['supplier'] for row in reviews}
    reviewed_suppliers = {row['supplier'] for row in reviews}
    suppliers_without_reviews = sorted(all_suppliers - reviewed_suppliers)

    return {
        'items': items,
        'issues': issues,
        'assignments_by_item': assignments_by_item,
        'assignments_by_pair': assignments_by_pair,
        'reviews': reviews,
        'current_by_pair': current_by_pair,
        'current_by_item': current_by_item,
        'rounds_by_pair': rounds_by_pair,
        'expert_history': expert_history,
        'expert_current': expert_current,
        'component_of_item': component_of_item,
        'component_of_supplier': component_of_supplier,
        'roots': roots,
        'groups': groups,
        'component_ids': component_ids,
        'suppliers_without_reviews': suppliers_without_reviews,
        'all_suppliers': sorted(all_suppliers),
    }


def _votes(item_id, prepared):
    return prepared['current_by_item'].get(item_id, [])


def _degree(item_id, prepared):
    assigned = {row['supplier'] for row in prepared['assignments_by_item'].get(item_id, [])}
    reviewed = {row['supplier'] for row in _votes(item_id, prepared)}
    return len(assigned | reviewed) or 1


def _item_context(item_id, prepared):
    return _context_of(prepared['assignments_by_item'].get(item_id), _degree(item_id, prepared))


def _effective_expert(prepared, item_id, revision_mode):
    """选择用于锚定的专家版本；previous 仅用于修订敏感性，不进入主结论。"""
    history = [row for row in prepared['expert_history'].get(item_id, []) if row['_valid']]
    if revision_mode == 'previous' and len(history) >= 2:
        return history[-2]
    return prepared['expert_current'].get(item_id)


def _anchor_records(prepared, revision_mode='current'):
    """只用专家版本构造锚点；弃判复核不计入准确率，只记录弃判次数。"""
    anchors = defaultdict(list)
    abstentions = defaultdict(int)
    for item_id in prepared['expert_current']:
        expert = _effective_expert(prepared, item_id, revision_mode)
        expert_label = expert.get('expert_label')
        if not expert['_valid'] or expert_label == ABSTAIN:
            continue
        item = prepared['items'][item_id]
        context = _item_context(item_id, prepared)
        history = [row for row in prepared['expert_history'].get(item_id, []) if row['_valid']]
        previous_labels = [row.get('expert_label') for row in history
                           if (row.get('revision', 0), row.get('received_at', ''))
                           < (expert.get('revision', 0), expert.get('received_at', ''))]
        for vote in _votes(item_id, prepared):
            if not vote['_valid']:
                continue
            if vote['label'] == ABSTAIN:
                abstentions[vote['supplier']] += 1
                continue
            anchors[vote['supplier']].append({
                'item_id': item_id,
                'supplier': vote['supplier'],
                'label': vote['label'],
                'expert_label': expert_label,
                'expert_revision': expert.get('revision', 0),
                'expert_received_at': expert.get('received_at'),
                'correct': vote['label'] == expert_label,
                'prior_correct': (vote['label'] == previous_labels[-1]
                                  if previous_labels else None),
                'stratum': item['stratum'],
                'categories': item['categories'],
                'degree': context['degree'],
                'probability': context['probability'],
                'round': vote['round'],
                'revision': vote.get('revision', 0),
            })
    return anchors, abstentions


def _summary(records, prior_mean, prior_strength):
    count_records = [record for record in records if record['correct'] is not None]
    total = len(count_records)
    correct = sum(1 for record in count_records if record['correct'])
    accuracy = _fraction(correct, total)
    chance = (sum(_chance(record['categories']) for record in count_records) / total
              if total else prior_mean)
    kappa = ((accuracy - chance) / (1 - chance)
             if accuracy is not None and chance < 1 else None)
    return {
        'anchors': total,
        'correct': correct,
        'accuracy': accuracy,
        'chance_accuracy': _round(chance),
        'kappa': _round(kappa),
        'wilson_95': wilson(correct, total),
        'smoothed': _smoothed(count_records, prior_mean),
        'prior_mean': _round(prior_mean),
        'prior_strength': prior_strength,
    }


def _group_by(records, key):
    grouped = defaultdict(list)
    for record in records:
        grouped[key(record)].append(record)
    return grouped


def _context_of_record(record):
    return f"{record['degree']}|{record['probability']}"


def _reliability(prepared, min_anchors):
    anchors, abstentions = _anchor_records(prepared)
    estimates = {}
    for supplier in prepared['all_suppliers']:
        records = anchors.get(supplier, [])
        chance = (sum(_chance(record['categories']) for record in records) / len(records)
                  if records else 0.5)
        global_summary = _summary(records, chance, PRIOR_STRENGTH)
        strata = {}
        for stratum, rows in sorted(_group_by(records, lambda row: row['stratum']).items()):
            strata[stratum] = {
                'support': 'supported' if len(rows) >= min_anchors else 'provisional',
                **_summary(rows, global_summary['smoothed'] or chance, 3.0),
                'items': sorted({row['item_id'] for row in rows}, key=_natural_key),
            }
        contexts = {}
        for context, rows in sorted(_group_by(records, _context_of_record).items()):
            contexts[context] = {
                'context': context,
                'support': 'supported' if len(rows) >= min_anchors else 'provisional',
                **_summary(rows, global_summary['smoothed'] or chance, 4.0),
                'items': sorted({row['item_id'] for row in rows}, key=_natural_key),
            }
        rounds = {}
        for round_key, rows in sorted(_group_by(records, lambda row: row['round']).items(),
                                      key=lambda pair: int(pair[0])):
            rounds[str(round_key)] = {
                'anchors': len(rows),
                'accuracy': _fraction(sum(1 for row in rows if row['correct']), len(rows)),
                'items': sorted({row['item_id'] for row in rows}, key=_natural_key),
            }
        by_class = {}
        for expert_class, rows in sorted(
                _group_by(records, lambda row: row['expert_label']).items(),
                key=lambda pair: _natural_key(pair[0])):
            by_class[expert_class] = {
                'support': 'supported' if len(rows) >= min_anchors else 'provisional',
                **_summary(rows, global_summary['smoothed'] or chance, 3.0),
                'items': sorted({row['item_id'] for row in rows}, key=_natural_key),
            }
        reviewed_pairs = len({(row['item_id'], row['supplier'], row['round'])
                              for row in prepared['reviews'] if row['supplier'] == supplier})
        repeat_reviews = sum(1 for pair, rounds_seen in prepared['rounds_by_pair'].items()
                             if pair[1] == supplier and len(rounds_seen) > 1)
        abstained = sum(1 for row in prepared['reviews']
                        if row['supplier'] == supplier and row['label'] == ABSTAIN)
        support = ('supported' if global_summary['anchors'] >= min_anchors
                   else 'provisional' if global_summary['anchors'] else 'unidentified')
        global_summary['support'] = support
        confusion = _confusion(records)
        mapping_suspect = bool(confusion['mapping_diagnosis']['suspect'])
        if mapping_suspect:
            support = 'mapping_review_required'
            global_summary['support'] = support
        estimates[supplier] = {
            'supplier': supplier,
            'support': support,
            'component': prepared['component_of_supplier'].get(supplier),
            'global': global_summary,
            'by_stratum': strata,
            'by_context': contexts,
            'by_expert_class': by_class,
            'by_round': rounds,
            'review_records': reviewed_pairs,
            'repeat_review_pairs': repeat_reviews,
            'abstentions': abstained,
            'abstention_rate': _fraction(abstained, reviewed_pairs),
            'anchored_abstentions': abstentions.get(supplier, 0),
            'anchor_items': sorted({record['item_id'] for record in records}, key=_natural_key),
            'confusion': confusion,
            'mapping_suspect': mapping_suspect,
            '_records': records,
        }
    return estimates


def _confusion(records):
    table = Counter((record['expert_label'], record['label']) for record in records)
    total = sum(table.values())
    cells = [{'expert_label': expert, 'supplier_label': label, 'count': count,
              'share': _fraction(count, total)}
             for (expert, label), count in sorted(table.items())]
    wrong = [cell for cell in cells if cell['expert_label'] != cell['supplier_label']]
    for cell in cells:
        cell['correct'] = cell['expert_label'] == cell['supplier_label']
    diagnosis = _mapping_diagnosis(records, total)
    if not wrong:
        return {'total': total, 'cells': cells, 'systematic_pattern': False,
                'dominant_error': None, 'mapping_diagnosis': diagnosis,
                'note': ('锚点内没有错误，需警惕样本过小。' if total else '没有可用的锚点。')}
    by_mapping = defaultdict(int)
    for record in records:
        if not record['correct']:
            by_mapping[(record['expert_label'], record['label'])] += 1
    dominant_key, dominant_count = max(by_mapping.items(), key=lambda item: item[1])
    wrong_total = sum(by_mapping.values())
    systematic = dominant_count >= 0.8 * wrong_total
    return {
        'total': total,
        'cells': cells,
        'systematic_pattern': systematic,
        'dominant_error': {'expert_label': dominant_key[0], 'supplier_label': dominant_key[1],
                           'count': dominant_count, 'share_of_errors': _fraction(dominant_count, wrong_total)},
        'mapping_diagnosis': diagnosis,
        'note': diagnosis['note'] if diagnosis['suspect'] else (
            '错误集中在单一“专家→供应商”类别映射上，较像系统性偏移而非随机噪声。'
            if systematic else '错误较分散，未发现单一主导映射。'),
    }


def _mapping_diagnosis(records, total):
    """识别“类别编码整体错位”这一类系统性映射，而不是把它当作供应商能力。"""
    base = {'suspect': False, 'best_mapping': None, 'match_rate': None,
            'diagonal_rate': None, 'note': '未发现稳定的整体类别映射。'}
    if total < 5:
        return {**base, 'note': '锚点太少，无法判断是否存在整体类别映射。'}
    expert_labels = sorted({record['expert_label'] for record in records}, key=_natural_key)
    supplier_labels = sorted({record['label'] for record in records}, key=_natural_key)
    if len(expert_labels) < 2:
        return {**base, 'note': '专家锚点只覆盖一个类别，无法识别映射错位。'}
    table = Counter((record['expert_label'], record['label']) for record in records)
    diagonal = sum(table[(label, label)] for label in expert_labels)
    diagonal_rate = diagonal / total
    best_rate, best_mapping = -1.0, None
    if len(supplier_labels) <= 7:
        for candidate in permutations(supplier_labels):
            mapping = dict(zip(expert_labels, candidate))
            matched = sum(count for (expert, label), count in table.items()
                          if mapping.get(expert) == label)
            if matched / total > best_rate:
                best_rate, best_mapping = matched / total, mapping
    if best_mapping is None:
        return {**base, 'diagonal_rate': _round(diagonal_rate)}
    changed = [expert for expert, label in best_mapping.items() if expert != label]
    suspect = bool(best_rate >= 0.8 and diagonal_rate <= 0.5 and changed)
    return {
        'suspect': suspect,
        'best_mapping': best_mapping,
        'match_rate': _round(best_rate),
        'diagonal_rate': _round(diagonal_rate),
        'note': ('存在稳定的整体类别映射：按该映射后一致率为 '
                 f'{best_rate:.0%}，而原样一致率仅 {diagonal_rate:.0%}。'
                 '这更像类别编码/口径不一致，应先核对映射；在核对前不应把该供应商'
                 '当作低质量供应商排名，也不要静默反推其标签。'
                 if suspect else '未发现稳定的整体类别映射。'),
    }



def _reliability_cell(estimate, stratum, context_key):
    """同层锚点优先；否则同派单情境；最后才用全局，并标明来源。

    类别映射疑似整体错位的供应商不进入加权：那不是能力估计，先要核对口径。
    """
    if estimate is None:
        return None, 'no_anchor'
    if estimate['support'] == 'mapping_review_required':
        return None, 'mapping_review_required'
    cell = estimate['by_stratum'].get(stratum)
    if cell and cell['support'] == 'supported':
        return cell, 'stratum'
    context = estimate['by_context'].get(context_key)
    if context and context['support'] == 'supported':
        return context, 'context'
    if estimate['global']['support'] == 'supported':
        return estimate['global'], 'global'
    if estimate['global']['anchors']:
        return estimate['global'], 'provisional_global'
    return None, 'no_anchor'


def _standardise(estimate, reference_weights, field='by_stratum', label='stratum'):
    """按共同构成直接标准化；锚点不足的单元不进入加权，也不补齐。"""
    parts, missing = [], []
    weighted = total_weight = 0.0
    for key, weight in reference_weights.items():
        cell = estimate[field].get(key)
        if not cell or cell['support'] != 'supported':
            missing.append(key)
            continue
        parts.append({label: key, 'weight': _round(weight),
                      'reliability': cell['smoothed'], 'anchors': cell['anchors']})
        weighted += weight * cell['smoothed']
        total_weight += weight
    if not parts:
        return {'value': None, 'status': 'unidentified', 'parts': parts,
                'missing_units': missing,
                'note': '没有任何单元达到锚点下限，不能标准化或跨供应商比较。'}
    return {
        'value': _round(weighted / total_weight),
        'status': 'complete' if not missing else 'partial',
        'parts': parts,
        'missing_units': missing,
        'note': ('按共同构成权重直接标准化。' if not missing
                 else '部分单元锚点不足，标准化只覆盖已支持的单元，不能据此补齐缺失单元。'),
    }


# ---------------------------------------------------------------------------
# 逐资料共识：按锚定可靠度加权，并保留完整来源与不确定性

def _source_records(estimate, source, stratum, context_key):
    records = estimate['_records']
    if source == 'stratum':
        return [row for row in records if row['stratum'] == stratum]
    if source == 'context':
        return [row for row in records if _context_of_record(row) == context_key]
    return list(records)


def _supplier_probability(estimate, stratum, context_key, item_id, supplier):
    """取该供应商在该资料上的折算概率。

    只用同一可靠度来源的锚点，并排除该资料自身的锚点记录避免泄漏。
    若该层没有锚点，返回 None 而不是借用其它层，避免把不同难度混为一谈。
    """
    cell, source = _reliability_cell(estimate, stratum, context_key)
    if cell is None:
        return {'supplier': supplier, 'probability': None, 'item_reliability': None,
                'reliability_source': source,
                'reliability_support': estimate['support'] if estimate else 'unidentified',
                'reliability_note': _cell_note(source),
                'anchors_used': 0, 'anchors_at_source': 0}
    rows = [row for row in _source_records(estimate, source, stratum, context_key)
            if not (row['item_id'] == item_id and row['supplier'] == supplier)]
    if not rows:
        return {'supplier': supplier, 'probability': None, 'item_reliability': None,
                'reliability_source': source + '_no_holdout',
                'reliability_support': cell.get('support', estimate['support']),
                'reliability_note': '该资料是此来源唯一的锚点，去掉后无可用估计。',
                'anchors_used': 0, 'anchors_at_source': cell.get('anchors', 0)}
    # Jeffreys 先验：小样本时不让单条锚点直接给出接近 1 的概率。
    correct = sum(1 for row in rows if row['correct'])
    alpha = 0.5 + correct
    beta = 0.5 + (len(rows) - correct)
    probability = alpha / (alpha + beta)
    return {
        'supplier': supplier,
        'probability': _round(probability),
        'item_reliability': _round(probability),
        'alpha': _round(alpha, 4),
        'beta': _round(beta, 4),
        'reliability_source': source,
        'reliability_support': cell.get('support', estimate['support']),
        'reliability_note': None,
        'table_reliability': cell.get('smoothed'),
        'anchors_used': len(rows),
        'anchors_at_source': cell.get('anchors'),
    }


def _cell_note(source):
    if source == 'mapping_review_required':
        return '该供应商的类别映射疑似整体错位，先核对口径，不进入共识加权。'
    if source == 'no_anchor':
        return '该供应商没有专家锚点，不能估计可靠度，也不参与加权。'
    return None


def _posterior(votes, categories, probabilities):
    """一币模型：真类以 p 被投中，投错时其余类别等可能。"""
    if len(categories) <= 1:
        return {category: 1.0 for category in categories}
    informative = [(vote, probabilities.get(vote['supplier']))
                   for vote in votes
                   if probabilities.get(vote['supplier']) is not None
                   and vote['label'] in categories]
    if not informative:
        return None
    log_scores = {category: 0.0 for category in categories}
    for vote, probability in informative:
        for category in categories:
            if category == vote['label']:
                log_scores[category] += math.log(probability)
            else:
                log_scores[category] += math.log((1 - probability) / (len(categories) - 1))
    peak = max(log_scores.values())
    weights = {category: math.exp(score - peak) for category, score in log_scores.items()}
    total = sum(weights.values())
    return {category: weight / total for category, weight in weights.items()}


def _ordered(posterior):
    return sorted(posterior.items(), key=lambda pair: (-pair[1], _natural_key(pair[0])))


def _monte_carlo(item_id, votes, categories, parameters, seed):
    if len(categories) <= 1:
        return {'draws': MC_DRAWS, 'winner_frequency': {categories[0]: 1.0} if categories else {},
                'top_probability_95': [1.0, 1.0] if categories else [None, None]}
    generator = random.Random(_seed(seed, item_id))
    winners = Counter()
    top_values = []
    for _ in range(MC_DRAWS):
        sampled = {supplier: generator.betavariate(alpha, beta)
                   for supplier, (alpha, beta) in parameters.items()}
        posterior = _posterior(votes, categories, sampled)
        if posterior is None:
            continue
        ordered = _ordered(posterior)
        winners[ordered[0][0]] += 1
        top_values.append(ordered[0][1])
    total = sum(winners.values()) or 1
    return {
        'draws': MC_DRAWS,
        'winner_frequency': {category: _fraction(count, total)
                             for category, count in sorted(winners.items(),
                                                           key=lambda pair: (-pair[1], pair[0]))},
        'top_probability_95': [_percentile(top_values, 0.025), _percentile(top_values, 0.975)],
        'note': '区间来自供应商锚点准确率的参数自助法，只覆盖可靠度估计的不确定性。',
    }


def _vote_summary(votes, categories):
    labels = [vote['label'] for vote in votes if vote['label'] in categories]
    counts = Counter(labels)
    total = sum(counts.values())
    shares = {label: _fraction(count, total) for label, count in
              sorted(counts.items(), key=lambda pair: (-pair[1], _natural_key(pair[0])))}
    entropy = 0.0
    if total > 1 and len(counts) > 1:
        entropy = -sum((count / total) * math.log(count / total) for count in counts.values()) / math.log(len(counts))
    return {
        'valid': total,
        'by_label': shares,
        'distinct': len(counts),
        'entropy': _round(entropy),
        'plurality': (max(sorted(counts, key=_natural_key), key=lambda label: counts[label])
                      if counts else None),
        'tie': (len({count for count in counts.values()}) == 1 and len(counts) > 1) if counts else False,
    }


def _source_bundle(item_id, prepared):
    assignments = []
    for row in sorted(prepared['assignments_by_item'].get(item_id, []),
                      key=lambda row: _natural_key(row['supplier'])):
        assignments.append({'supplier': row['supplier'], 'batch': row.get('batch'),
                            'probability': row.get('probability'),
                            'received_at': row.get('received_at')})
    reviews = []
    for row in _votes(item_id, prepared):
        reviews.append({'supplier': row['supplier'], 'round': row['round'],
                        'revision': row.get('revision', 0), 'label': row.get('label'),
                        'received_at': row.get('received_at')})
    experts = []
    for row in sorted(prepared['expert_history'].get(item_id, []),
                      key=lambda row: (row.get('revision', 0), row.get('received_at', ''))):
        experts.append({'expert_label': row.get('expert_label'),
                        'revision': row.get('revision', 0),
                        'received_at': row.get('received_at')})
    return {'assignments': assignments, 'reviews': reviews, 'expert_versions': experts}


def _difference_rows(votes, weights, posterior, recommendation, status, leading=None):
    """逐个分歧类别说明证据状态，而不是只给一个多数票。"""
    rows = []
    grouped = defaultdict(list)
    for vote in votes:
        if vote['label'] in ('abstain', None):
            continue
        grouped[vote['label']].append(vote['supplier'])
    reference = recommendation or leading
    top = (posterior or {}).get(reference) if reference else None
    for label in sorted(grouped, key=_natural_key):
        suppliers = sorted(grouped[label], key=_natural_key)
        probability = _round((posterior or {}).get(label)) if posterior else None
        excluded = {supplier: weights[supplier]['reliability_source']
                    for supplier in suppliers
                    if not weights[supplier].get('informative')}
        if recommendation is None:
            evidence = ('low_confidence_unresolved' if status == 'low_confidence'
                        else 'consensus_unresolved')
        elif label == recommendation:
            evidence = 'supports_recommendation'
        elif excluded:
            evidence = 'excluded_from_weighting'
        elif status in {'supported', 'supported_single_source'}:
            evidence = 'outweighed_by_stronger_evidence'
        else:
            evidence = 'not_resolved'
        rows.append({
            'label': label,
            'suppliers': suppliers,
            'posterior': probability,
            'posterior_gap_to_top': (_round(top - probability)
                                     if top is not None and probability is not None else None),
            'supplier_reliability': {supplier: weights.get(supplier, {}).get('item_reliability')
                                     for supplier in suppliers},
            'excluded_suppliers': excluded or None,
            'evidence': evidence,
            'evidence_note': _evidence_note(evidence, excluded),
        })
    return rows


def _evidence_note(evidence, excluded):
    if evidence == 'supports_recommendation':
        return '该类别获得被采纳的加权证据支持。'
    if evidence == 'excluded_from_weighting':
        reasons = ','.join(sorted(set(excluded.values())))
        return f'该类别只由未纳入加权的供应商提出（{reasons}），不能据此推翻建议。'
    if evidence == 'outweighed_by_stronger_evidence':
        return '该类别有供应商支持，但其锚定可靠度低于领先类别；差异有证据但不足以改变结论。'
    if evidence == 'not_resolved':
        return '该差异目前没有被可靠度或专家结论裁决，需要补证据。'
    if evidence == 'low_confidence_unresolved':
        return '领先类别不确定，尚未形成可对外给出的建议；该差异仍需专家或补复核裁决。'
    return '尚无可用于裁决该差异的加权证据或专家锚点。' 



def _item_analysis(item_id, prepared, estimates, options):
    item = prepared['items'][item_id]
    votes_all = _votes(item_id, prepared)
    valid_votes = [vote for vote in votes_all if vote['_valid']]
    abstentions = [vote for vote in valid_votes if vote['label'] == ABSTAIN]
    votes = [vote for vote in valid_votes if vote['label'] != ABSTAIN]
    context = _item_context(item_id, prepared)
    context_key = _context_key(context)
    stratum = item['stratum']
    chance = _chance(item['categories'])

    repeated = {vote['supplier']: sorted(prepared['rounds_by_pair'][(item_id, vote['supplier'])])
                for vote in votes
                if len(prepared['rounds_by_pair'][(item_id, vote['supplier'])]) > 1}
    weights = {}
    parameters = {}
    for vote in votes:
        estimate = estimates.get(vote['supplier'])
        cell = _supplier_probability(estimate, stratum, context_key, item_id, vote['supplier'])
        probability = cell['probability']
        informative = probability is not None and probability > chance
        weights[vote['supplier']] = {
            **cell,
            'informative': informative,
            'influence': (_round(max(0.0, (probability - chance) / (1 - chance)))
                          if informative and chance < 1 else 0.0),
        }
        if informative:
            parameters[vote['supplier']] = (cell['alpha'], cell['beta'])

    effective = {supplier: cell['probability'] for supplier, cell in weights.items()
                 if cell['informative']}
    posterior = _posterior(votes, item['categories'], effective)
    vote_summary = _vote_summary(votes, item['categories'])
    expert = prepared['expert_current'].get(item_id)
    expert_label = expert.get('expert_label') if expert and expert['_valid'] else None
    recommendation = None
    monte_carlo = None
    sensitivity = None
    confidence = None
    top_probability = None

    if not votes_all:
        status = 'no_review'
    elif not valid_votes:
        status = 'invalid_review_records'
    elif not votes:
        status = 'abstained'
    elif posterior is None:
        status = 'unverified'
    else:
        ordered = _ordered(posterior)
        top_label, top_probability = ordered[0]
        posterior_rows = [{'label': label, 'probability': _round(probability)}
                          for label, probability in ordered]
        monte_carlo = _monte_carlo(item_id, votes, item['categories'], parameters, options['seed'])
        lower = (monte_carlo['top_probability_95'] or [None])[0]
        sensitivity = {'leave_one_supplier_out': []}
        if len(effective) >= 2:
            for supplier in sorted(effective, key=_natural_key):
                reduced = {key: value for key, value in effective.items() if key != supplier}
                reduced_ordered = _ordered(_posterior(votes, item['categories'], reduced))
                sensitivity['leave_one_supplier_out'].append({
                    'excluded': supplier,
                    'label': reduced_ordered[0][0],
                    'probability': _round(reduced_ordered[0][1]),
                    'same_as_full': reduced_ordered[0][0] == top_label,
                })
        changes = [row for row in sensitivity['leave_one_supplier_out'] if not row['same_as_full']]
        sensitivity['fragile'] = bool(changes)
        sensitivity['note'] = ('去掉任一有信息供应商后结论不变。' if not changes
                               else '去掉部分供应商会改变结论，该资料不宜作为稳定结论对外。')
        if top_probability < options['consensus_threshold']:
            status, confidence = 'contested', 'low'
        elif changes:
            status, confidence = 'contested', 'low'
        elif lower is None or lower < options['lower_confidence_floor']:
            # 区间下界不足时只保留“领先类别”，不对外给 recommendation。
            status, confidence = 'low_confidence', 'low'
        elif len(effective) >= 2:
            status, confidence = 'supported', 'high'
            recommendation = top_label
        else:
            status, confidence = 'supported_single_source', 'medium'
            recommendation = top_label

    if len(item['categories']) == 1:
        # 只有一个合法类别时结论由定义决定，不需要统计共识，也不应算作高置信预测。
        status, confidence = 'single_category', 'definitional'
        recommendation = item['categories'][0]
        needs_expert = False

    if posterior is not None:
        ordered = _ordered(posterior)
        posterior_rows = [{'label': label, 'probability': _round(probability)}
                          for label, probability in ordered]
    else:
        posterior_rows = []

    if len(item['categories']) > 1:
        needs_expert = status in {'contested', 'low_confidence', 'unverified',
                                  'abstained', 'no_review', 'invalid_review_records'}
    sources = _source_bundle(item_id, prepared)
    component = prepared['component_of_item'].get(item_id)
    root = _root_of(prepared, item_id)
    group_suppliers = prepared['groups'][root]['suppliers'] if root in prepared['groups'] else set()
    component_anchored = any(estimates[supplier]['global']['anchors'] for supplier in group_suppliers)

    return {
        'item_id': item_id,
        'stratum': stratum,
        'categories': item['categories'],
        'component': component,
        'assignment_context': context,
        'status': status,
        'confidence': confidence,
        'needs_expert': needs_expert,
        'recommendation': recommendation,
        'leading_label': posterior_rows[0]['label'] if posterior_rows else None,
        'leading_probability': posterior_rows[0]['probability'] if posterior_rows else None,
        'posterior': posterior_rows,
        'monte_carlo': monte_carlo,
        'sensitivity': sensitivity,
        'vote_summary': vote_summary,
        'repeat_rounds': repeated,
        'repeat_round_note': ('同一供应商在同一资料上有多个 round；只取最新 round，'
                              '旧 round 不重复计票。' if repeated else None),
        'anchor': {
            'component_has_expert_anchor': component_anchored,
            'expert_label': expert_label,
            'expert_revision': expert.get('revision', 0) if expert else None,
            'not_used_in_consensus': True,
            'agrees_with_recommendation': (expert_label == recommendation
                                           if expert_label and recommendation else None),
        },
        'abstentions': [{'supplier': vote['supplier'], 'round': vote['round'],
                         'revision': vote.get('revision', 0)} for vote in abstentions],
        'supplier_weights': [weights[supplier] for supplier in sorted(weights, key=_natural_key)],
        'cross_check_available': sum(1 for cell in weights.values()
                                     if cell.get('informative')) >= 2,
        'differences': _difference_rows(votes, weights, posterior, recommendation, status,
                                        leading=posterior_rows[0]['label']
                                        if posterior_rows else None),
        'sources': sources,
        'trace_digest': _digest({'item_id': item_id, 'status': status,
                                 'categories': item['categories'],
                                 'sources': sources}),
        'interpretation': _interpretation(status, component_anchored),
    }


def _root_of(prepared, item_id):
    return prepared['roots'].get(item_id)


def _digest(value):
    import json
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _interpretation(status, anchored):
    if status == 'single_category':
        return '该资料只有唯一合法类别，结论由类别定义决定，不是统计推断。'
    if status == 'no_review':
        return '该资料没有任何复核；不能给出建议，应进入专家或复核队列。'
    if status == 'invalid_review_records':
        return '该资料的复核记录类别不合法，已全部隔离；需先修正数据再谈共识。'
    if status == 'abstained':
        return '当前复核全部为弃判；弃判是明确信息，不能折算成任一类别，也不等于未派单。'
    if status == 'unverified':
        return ('该资料所在分组没有可用的专家锚点，供应商之间也没有可依赖的可靠度；'
                '只能报告分歧，不能据此排序供应商或给出共识概率。')
    if status == 'contested':
        return ('后验未达阈值，或去掉任一供应商就翻转结论；'
                '应补专家复核，而不是把当前领先类别当作结论。')
    if status == 'low_confidence':
        return ('后验点估计较高，但区间下界不足；只能作为待核实的领先类别，'
                '不应直接对外称为共识结论。')
    if status == 'supported_single_source':
        return ('只有一个供应商被纳入加权，建议来自它的锚定可靠度；'
                '这是单源结论，不是多源共识，其他供应商的差异未被裁决。')
    if anchored:
        return '建议由多个有信息的供应商加权形成，且该分组存在专家锚点。'
    return '建议由多个有信息的供应商加权形成；该分组没有专家锚点，结论尚未校验。'


def _context_breakdown(prepared, estimates):
    rows = []
    for supplier in prepared['all_suppliers']:
        estimate = estimates[supplier]
        if estimate['support'] == 'unidentified':
            continue
        for context_key, cell in sorted(estimate['by_context'].items()):
            rows.append({
                'supplier': supplier,
                'context': context_key,
                'anchors': cell['anchors'],
                'accuracy': cell['accuracy'],
                'wilson_95': cell['wilson_95'],
                'support': cell['support'],
            })
    return {
        'rows': rows,
        'note': ('派单情境按“分派供应商数|派单概率”划分；情境间构成不同时，'
                 '不能用总体准确率互相替代。'),
    }


def _component_analysis(prepared, estimates, items):
    # 资料层构成来自全部资料；专家类别构成来自已有专家结论的资料。
    stratum_counts = Counter(item['stratum'] for item in prepared['items'].values())
    total = sum(stratum_counts.values()) or 1
    stratum_weights = {key: count / total for key, count in
                       sorted(stratum_counts.items(), key=lambda pair: _natural_key(pair[0]))}
    class_counts = Counter(expert.get('expert_label')
                           for expert in prepared['expert_current'].values()
                           if expert['_valid'] and expert.get('expert_label') != ABSTAIN)
    class_total = sum(class_counts.values()) or 1
    class_weights = {key: count / class_total for key, count in
                     sorted(class_counts.items(), key=lambda pair: _natural_key(pair[0]))}

    groups = {}
    by_component = defaultdict(list)
    for item in items:
        by_component[item['component']].append(item)
    for component, component_items in sorted(by_component.items(),
                                             key=lambda pair: _natural_key(pair[0])):
        suppliers = sorted({cell['supplier'] for item in component_items
                            for cell in item['supplier_weights']}, key=_natural_key)
        anchors = {supplier: estimates[supplier]['global']['anchors'] for supplier in suppliers
                   if supplier in estimates}
        anchored = any(value > 0 for value in anchors.values())
        entries = []
        excluded_from_ranking = []
        for supplier in suppliers:
            estimate = estimates[supplier]
            if estimate['mapping_suspect']:
                excluded_from_ranking.append({
                    'supplier': supplier,
                    'reason': 'mapping_review_required',
                    'note': estimate['confusion']['mapping_diagnosis']['note'],
                })
                continue
            by_stratum = _standardise(estimate, stratum_weights)
            by_class = _standardise(estimate, class_weights,
                                    field='by_expert_class', label='expert_class')
            entries.append({
                'supplier': supplier,
                'standardised_reliability': by_stratum['value'],
                'status': by_stratum['status'],
                'parts': by_stratum['parts'],
                'missing_strata': by_stratum['missing_units'],
                'class_standardised_reliability': by_class['value'],
                'class_status': by_class['status'],
                'class_parts': by_class['parts'],
                'missing_classes': by_class['missing_units'],
                'global_accuracy': estimate['global']['accuracy'],
                'global_anchors': estimate['global']['anchors'],
                'note': by_stratum['note'],
            })
        comparable_entries = [row for row in entries
                              if row['standardised_reliability'] is not None
                              and (not class_weights
                                   or row['class_standardised_reliability'] is not None)]
        for row in entries:
            row['rankable_in_group'] = row in comparable_entries
        comparable = anchored and len(comparable_entries) >= 2
        ranking = []
        if comparable:
            for row in comparable_entries:
                values = [row['standardised_reliability']]
                if row['class_standardised_reliability'] is not None:
                    values.append(row['class_standardised_reliability'])
                row['composite_reliability'] = _round(sum(values) / len(values))
            ranking = sorted(comparable_entries,
                             key=lambda row: (-row['composite_reliability'],
                                              _natural_key(row['supplier'])))
            for position, row in enumerate(ranking, 1):
                row['rank'] = position
        if not suppliers:
            status = 'no_assignment_or_review'
            note = '该资料没有派单也没有复核，不存在可比较的供应商证据。'
        elif not anchored:
            status = 'unidentified_no_anchor'
            note = ('该分组没有任何专家锚点，不能估计供应商可靠度，也不能给出高低排序；'
                    '这些供应商的准确率不能与有锚点分组直接比较。')
        elif len(suppliers) < 2:
            status = 'no_overlap'
            note = ('该分组只有一个供应商，没有可比的交叠资料，不能给出供应商高低排序。')
        elif not comparable:
            status = 'insufficient_comparable_suppliers'
            note = ('有锚点的供应商不足以在共同资料层上形成两个可标准化估计；'
                    '不能给出供应商高低排序，应先补锚点或补共同资料层交叠。')
        elif excluded_from_ranking:
            status = 'partially_comparable'
            note = ('分组内部分供应商的类别映射疑似整体错位，已排除出排名；'
                    '其余供应商按共同资料层构成标准化比较。')
        else:
            status = 'comparable'
            note = ('该分组有专家锚点且供应商间有交叠；排名使用资料层构成与专家类别构成'
                    '两个维度的标准化均值，避免把资料难度或类别组合差异算成供应商能力差异。')
        groups[component] = {
            'component': component,
            'suppliers': suppliers,
            'items': len(component_items),
            'expert_anchored': anchored,
            'anchor_counts': anchors,
            'comparable': comparable,
            'supplier_estimates': entries,
            'ranking': ranking,
            'excluded_from_ranking': excluded_from_ranking,
            'status': status,
            'note': note,
        }
    return groups


def _queue(items, prepared, estimates, options):
    """专家名额有限时的补复核排序；这是证据优先级，不是真值预测。"""
    slots = options['expert_slots']
    threshold = options['consensus_threshold']
    candidates = []
    for item in items:
        if not item['needs_expert'] and item['status'] != 'supported_single_source':
            continue
        root = _root_of(prepared, item['item_id'])
        suppliers = sorted(prepared['groups'][root]['suppliers']) if root in prepared['groups'] else []
        anchor_counts = {supplier: estimates[supplier]['global']['anchors']
                         for supplier in suppliers if supplier in estimates}
        has_anchor = any(count > 0 for count in anchor_counts.values())
        if item['status'] == 'invalid_review_records':
            priority, reason = 'data_quality', 'invalid_review_labels'
        elif not has_anchor:
            priority, reason = 'evidence_gap', 'no_expert_anchor_in_component'
        elif item['status'] == 'contested':
            priority, reason = 'disputed_with_anchor', 'disagreement_not_resolved'
        elif item['status'] == 'abstained':
            priority, reason = 'all_abstained', 'abstention_is_not_a_label'
        elif item['status'] == 'low_confidence':
            priority, reason = 'low_confidence', 'interval_below_floor'
        elif item['status'] == 'supported_single_source':
            priority, reason = 'single_source_unvalidated', 'no_second_informative_supplier'
        else:
            priority, reason = 'unverified', 'no_informative_supplier'
        probability = item['leading_probability'] or 0.0
        gap = max(0.0, threshold - probability)
        influence = max((cell.get('influence') or 0.0) for cell in item['supplier_weights']) \
            if item['supplier_weights'] else 0.0
        votes = item['vote_summary']['valid']
        # 名额有限时：先补没有锚点的证据缺口，再补已有锚点但仍未解决的争议；
        # 同优先级内，越接近阈值、牵动越多权重、没有有效票的资料越优先。
        score = (110 if priority == 'data_quality' else 0)
        score += (100 if priority == 'evidence_gap' else 0)
        score += (50 if priority == 'disputed_with_anchor' else 0)
        score += (30 if priority == 'all_abstained' else 0)
        score += (20 if priority == 'low_confidence' else 0)
        # 单源结论只能作为已有锚点但尚未交叉验证的补充核对，排在最后。
        score += (0 if priority != 'single_source_unvalidated' else -10)
        score += gap * 20 + influence * 10 + (5 if votes == 0 else 0)
        if item['status'] == 'abstained':
            score += 20
        candidates.append({
            'item_id': item['item_id'],
            'stratum': item['stratum'],
            'component': item['component'],
            'status': item['status'],
            'leading_label': item['leading_label'],
            'leading_probability': item['leading_probability'],
            'votes': votes,
            'priority': priority,
            'reason': reason,
            'anchor_counts': anchor_counts,
            'component_has_expert_anchor': has_anchor,
            'provides_recommendation': item['recommendation'] is not None,
            'score': _round(score, 3),
        })
    candidates.sort(key=lambda row: (-row['score'], _natural_key(row['item_id'])))
    for position, row in enumerate(candidates, 1):
        row['rank'] = position
        row['selected'] = position <= slots
    selected = [row for row in candidates if row['selected']]
    strata = sorted({item['stratum'] for item in items}, key=_natural_key)
    coverage = {}
    for stratum in strata:
        in_stratum = [item for item in items if item['stratum'] == stratum]
        with_anchor = [item for item in in_stratum
                       if item['anchor']['component_has_expert_anchor']]
        coverage[stratum] = {
            'items': len(in_stratum),
            'items_in_anchored_component': len(with_anchor),
            'items_in_unanchored_component': len(in_stratum) - len(with_anchor),
            'selected_for_expert': sum(1 for row in selected if row['stratum'] == stratum),
        }
    return {
        'slots': slots,
        'threshold': threshold,
        'criteria': ('名额分配按证据缺口而非猜测真值：先补所在分组完全没有专家锚点的资料，'
                     '再补已有锚点但后验未达阈值或结论脆弱的争议，然后补全弃判和无有效票资料。'
                     '同优先级内优先接近阈值、牵动较多供应商权重、复核票数较少的资料。'
                     '单源结论排在最后，只在仍有名额时补交叉验证。'
                     '得分只用于排序补复核顺序，不代表任何资料的真实类别。'),
        'priority_counts': dict(Counter(row['priority'] for row in candidates)),
        'selected_by_priority': dict(Counter(row['priority'] for row in selected)),
        'selected_by_stratum': dict(Counter(row['stratum'] for row in selected)),
        'uncovered_strata': sorted({row['stratum'] for row in candidates
                                    if row['priority'] == 'evidence_gap'}
                                   - {row['stratum'] for row in selected}),
        'candidates': candidates,
        'selected': [row['item_id'] for row in candidates if row['selected']],
        'not_selected': [row['item_id'] for row in candidates if not row['selected']],
        'candidate_count': len(candidates),
        'anchor_coverage_by_stratum': coverage,
        'note': ('若名额用尽后仍有 evidence_gap 资料未被选入，说明当前名额不足以覆盖'
                 '所有无锚点分组；应优先向这些分组补专家结论，而不是给它们排名。'),
    }


def _mapping_reconciliation(prepared, estimates):
    """类别映射疑点要单独核对口径，不能混进专家复核名额或供应能力排名。"""
    rows = []
    for supplier in prepared['all_suppliers']:
        estimate = estimates[supplier]
        if not estimate['mapping_suspect']:
            continue
        diagnosis = estimate['confusion']['mapping_diagnosis']
        affected = sorted({record['item_id'] for record in estimate['_records']},
                          key=_natural_key)
        rows.append({
            'supplier': supplier,
            'component': estimate['component'],
            'proposed_mapping': diagnosis['best_mapping'],
            'match_rate_under_mapping': diagnosis['match_rate'],
            'raw_match_rate': diagnosis['diagonal_rate'],
            'anchors': estimate['global']['anchors'],
            'affected_items': affected,
            'affected_item_count': len(affected),
            'action': ('先与供应商核对类别定义与编码方向并留存书面口径；'
                       '核对完成前该供应商不进入共识加权或可靠度排名。'),
        })
    return {
        'suppliers': rows,
        'note': ('这是数据口径问题，不是供应商能力问题。系统不会静默按映射反推标签；'
                 '需要人工确认后再以新的 request_id 重新分析。'),
    }


def _revision_sensitivity(prepared, estimates):
    """专家修订敏感性：用修订前专家版本重算供应商准确率，观察结论是否翻转。"""
    previous_anchors, _ = _anchor_records(prepared, revision_mode='previous')
    rows = []
    for supplier in prepared['all_suppliers']:
        current = estimates[supplier]['global']
        records = previous_anchors.get(supplier, [])
        if not records:
            continue
        chance = sum(_chance(record['categories']) for record in records) / len(records)
        previous = _summary(records, chance, PRIOR_STRENGTH)
        rows.append({
            'supplier': supplier,
            'current_accuracy': current['accuracy'],
            'current_anchors': current['anchors'],
            'pre_revision_accuracy': previous['accuracy'],
            'pre_revision_anchors': previous['anchors'],
            'changed': current['accuracy'] != previous['accuracy'],
        })
    return {
        'rows': rows,
        'note': ('只替换专家版本，不替换供应商复核；用于说明专家结论修订会改变可靠度，'
                 '不是给修订本身加分。'),
    }


def _calibration(items, prepared):
    """用当前专家版本做留出检查；样本小，仅作诊断，不作为准确率承诺。"""
    buckets = {f'{low:.1f}-{min(high, 1.0):.1f}': {'total': 0, 'correct': 0}
               for low, high in zip(CALIBRATION_EDGES, CALIBRATION_EDGES[1:])}
    for item in items:
        expert = prepared['expert_current'].get(item['item_id'])
        if not expert or not expert['_valid'] or expert.get('expert_label') == ABSTAIN:
            continue
        if item['recommendation'] is None:
            continue
        probability = item['leading_probability'] or 0.0
        for (low, high), key in zip(zip(CALIBRATION_EDGES, CALIBRATION_EDGES[1:]), buckets):
            if low <= probability < high:
                buckets[key]['total'] += 1
                buckets[key]['correct'] += int(item['recommendation'] == expert.get('expert_label'))
                break
    rows = []
    for key, cell in buckets.items():
        rows.append({'bucket': key, 'total': cell['total'], 'correct': cell['correct'],
                     'observed_accuracy': _fraction(cell['correct'], cell['total'])})
    return {
        'rows': rows,
        'note': ('按建议后验概率分桶后与专家版本对照。这是锚点资料上的留一诊断，'
                 '仍可能偏乐观，且样本很小；若观测准确率明显低于名义概率，'
                 '说明后验不是无条件准确率，只能作为相对排序与补证据线索。'),
    }


def analyse(snapshot, options=None):
    options = options or {}
    if not isinstance(options, dict):
        raise ValueError('options 须为对象')
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise ValueError('未知分析选项：' + ','.join(unknown))
    min_anchors = options.get('min_anchors', DEFAULT_MIN_ANCHORS)
    expert_slots = options.get('expert_slots', DEFAULT_EXPERT_SLOTS)
    threshold = options.get('consensus_threshold', DEFAULT_CONSENSUS_THRESHOLD)
    floor = options.get('lower_confidence_floor', DEFAULT_LOWER_CONFIDENCE_FLOOR)
    if not isinstance(min_anchors, int) or isinstance(min_anchors, bool) or min_anchors < 1:
        raise ValueError('min_anchors 须为正整数')
    if not isinstance(expert_slots, int) or isinstance(expert_slots, bool) or expert_slots < 0:
        raise ValueError('expert_slots 须为非负整数')
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
        raise ValueError('consensus_threshold 须在 (0,1] 内')
    if not isinstance(floor, (int, float)) or isinstance(floor, bool) or not 0 < floor <= 1:
        raise ValueError('lower_confidence_floor 须在 (0,1] 内')

    prepared = _prepare(snapshot)
    options = {'min_anchors': min_anchors, 'expert_slots': expert_slots,
               'consensus_threshold': threshold, 'lower_confidence_floor': floor,
               'seed': METHOD_VERSION}
    estimates = _reliability(prepared, min_anchors)
    items = [_item_analysis(item_id, prepared, estimates, options)
             for item_id in sorted(prepared['items'], key=_natural_key)]
    groups = _component_analysis(prepared, estimates, items)
    queue = _queue(items, prepared, estimates, options)

    status_counts = Counter(item['status'] for item in items)
    recommendation_items = [item for item in items if item['recommendation'] is not None]
    supplier_table = []
    for supplier in prepared['all_suppliers']:
        estimate = estimates[supplier]
        if estimate['support'] == 'unidentified':
            continue
        supplier_table.append({
            'supplier': supplier,
            'component': estimate['component'],
            'support': estimate['support'],
            'accuracy_interpretable': not estimate['mapping_suspect'],
            'accuracy_note': (estimate['confusion']['mapping_diagnosis']['note']
                              if estimate['mapping_suspect'] else None),
            'anchors': estimate['global']['anchors'],
            'accuracy': estimate['global']['accuracy'],
            'chance_accuracy': estimate['global']['chance_accuracy'],
            'kappa': estimate['global']['kappa'],
            'wilson_95': estimate['global']['wilson_95'],
            'smoothed': estimate['global']['smoothed'],
            'abstention_rate': estimate['abstention_rate'],
            'anchored_abstention_rate': _fraction(
                estimate['anchored_abstentions'], estimate['global']['anchors']
                + estimate['anchored_abstentions']),
            'by_stratum': {stratum: {'anchors': cell['anchors'], 'accuracy': cell['accuracy'],
                                     'support': cell['support'],
                                     'smoothed': cell['smoothed'],
                                     'wilson_95': cell['wilson_95']}
                           for stratum, cell in estimate['by_stratum'].items()},
            'by_context': {key: {'anchors': cell['anchors'], 'accuracy': cell['accuracy'],
                                 'support': cell['support']}
                           for key, cell in estimate['by_context'].items()},
            'by_expert_class': {key: {'anchors': cell['anchors'],
                                      'accuracy': cell['accuracy'],
                                      'support': cell['support'],
                                      'wilson_95': cell['wilson_95']}
                                for key, cell in estimate['by_expert_class'].items()},
            'repeat_review_pairs': estimate['repeat_review_pairs'],
            'confusion': estimate['confusion'],
        })
    return {
        'method': METHOD,
        'method_version': METHOD_VERSION,
        'provenance': {
            'review_identity': 'item_id+supplier+round; revision 取组内最新，不增加独立票数',
            'expert_identity': 'item_id; 取最新 revision 作为锚点，旧版本只用于敏感性',
            'assignment_context': '分派供应商数|派单概率',
            'reliability_estimand': '专家锚点上的类别准确率，非无条件准确率',
            'consensus_estimand': '给定锚定可靠度与本次票数下的相对支持度',
        },
        'options': {'min_anchors': min_anchors, 'expert_slots': expert_slots,
                    'consensus_threshold': threshold,
                    'lower_confidence_floor': floor},
        'summary': {
            'items': len(items),
            'reviewed_items': sum(1 for item in items
                                  if item['sources']['reviews']),
            'recommended_items': len(recommendation_items),
            'needs_expert': sum(1 for item in items if item['needs_expert']),
            'status_counts': dict(sorted(status_counts.items())),
            'suppliers': len(prepared['all_suppliers']),
            'components': len(groups),
            'suppliers_without_reviews': prepared['suppliers_without_reviews'],
            'expert_anchor_items': len(prepared['expert_current']),
        },
        'supplier_reliability': supplier_table,
        'components': groups,
        'items': items,
        'expert_queue': queue,
        'mapping_reconciliation': _mapping_reconciliation(prepared, estimates),
        'context_breakdown': _context_breakdown(prepared, estimates),
        'revision_sensitivity': _revision_sensitivity(prepared, estimates),
        'calibration': _calibration(items, prepared),
        'interpretation': {
            'consensus_probability': ('建议概率是“在给定供应商锚定可靠度和本资料复核票数下的'
                                      '相对支持度”，不是无条件准确率，也不等同于专家结论。'),
            'expert_anchor': ('expert_versions 是按时间版本的专业结论，只在存在时作为锚点；'
                              '没有专家版本的资料不能当作已验证。'),
            'ranking': ('没有专家锚点、没有交叠、或可标准化供应商不足两家时不给排名；'
                        '有锚点的分组按资料层与专家类别构成标准化后才可比较。'),
            'revision': ('复核和专家版本的 revision 是同一身份的订正，不增加独立票数；'
                         '新复核应使用新的 request_id 重新计算。'),
        },
        'issues': prepared['issues'],
    }


def _compact_queue(queue):
    """精简视图只保留名额内名单和少量候补；完整队列仍在分析记录里。"""
    waiting = [row for row in queue['candidates'] if not row['selected']][:10]
    return {
        'slots': queue['slots'],
        'threshold': queue['threshold'],
        'criteria': queue['criteria'],
        'priority_counts': queue['priority_counts'],
        'selected_by_priority': queue['selected_by_priority'],
        'selected_by_stratum': queue['selected_by_stratum'],
        'anchor_coverage_by_stratum': queue['anchor_coverage_by_stratum'],
        'uncovered_strata': queue['uncovered_strata'],
        'selected': queue['selected'],
        'waiting_sample': waiting,
        'candidate_count': queue['candidate_count'],
        'note': queue['note'],
    }


def summarise(result, limit=20):
    """面向续约讨论的精简视图；完整明细仍保存在结果本身。"""
    # 关注列表按专家队列的补证据优先级排序，而不是按资料编号。
    by_id = {item['item_id']: item for item in result['items']}
    ordered = [entry for entry in result['expert_queue']['candidates']
               if by_id[entry['item_id']]['needs_expert']]
    rows = ordered if limit is None else ordered[:limit]
    return {
        'method': result['method'],
        'method_version': result['method_version'],
        'summary': result['summary'],
        'supplier_reliability': result['supplier_reliability'],
        'components': result['components'],
        'expert_queue': _compact_queue(result['expert_queue']),
        'attention_total': len(ordered),
        'attention': [{'item_id': row['item_id'], 'stratum': row['stratum'],
                       'status': row['status'], 'priority': row['priority'],
                       'recommendation': by_id[row['item_id']]['recommendation'],
                       'leading_probability': row['leading_probability'],
                       'selected_for_expert': row['selected']}
                      for row in rows],
        'mapping_reconciliation': result['mapping_reconciliation'],
        'revision_sensitivity': result['revision_sensitivity'],
        'calibration': result['calibration'],
        'interpretation': result['interpretation'],
    }
