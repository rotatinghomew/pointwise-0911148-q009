import copy
import tempfile
import unittest
from pathlib import Path

from app.consensus import analyse
from app.domain import seed_dataset
from app.repository import Conflict
from app.service import Service

AS_OF = '2026-09-30T23:59:59Z'


def item(result, item_id):
    return next(row for row in result['items'] if row['item_id'] == item_id)


def supplier(result, supplier_id):
    return next(row for row in result['supplier_reliability']
                if row['supplier'] == supplier_id)


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        self.dataset = seed_dataset()
        self.result = analyse(self.dataset)

    def test_recommendations_are_traceable_to_source_records(self):
        for row in self.result['items']:
            self.assertIn('sources', row)
            self.assertIn('trace_digest', row)
            self.assertEqual(row['sources']['reviews'],
                             [{'supplier': review['supplier'], 'round': review['round'],
                               'revision': review['revision'], 'label': review['label'],
                               'received_at': review['received_at']}
                              for review in row['sources']['reviews']])
            self.assertIsInstance(row['trace_digest'], str)
            if row['recommendation'] is not None:
                self.assertTrue(row['sources']['reviews'])
                self.assertTrue(row['sources']['assignments'] or row['sources']['expert_versions'])
        digests = {row['trace_digest'] for row in self.result['items']}
        self.assertEqual(len(digests), len(self.result['items']))

    def test_revisions_are_not_counted_as_extra_votes(self):
        row = item(self.result, 'i1')
        s0 = next(weight for weight in row['supplier_weights'] if weight['supplier'] == 's0')
        self.assertEqual(len([review for review in row['sources']['reviews']
                              if review['supplier'] == 's0']), 1)
        revised = next(review for review in row['sources']['reviews']
                       if review['supplier'] == 's0')
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(revised['label'], 'c2')
        self.assertIn(s0['supplier'], {weight['supplier'] for weight in row['supplier_weights']})

    def test_abstentions_are_not_treated_as_missing_assignments(self):
        row = item(self.result, 'i0')
        self.assertEqual(row['status'], 'abstained')
        self.assertIsNone(row['recommendation'])
        self.assertEqual(len(row['abstentions']), 2)
        self.assertEqual(row['assignment_context']['degree'], 2)
        self.assertTrue(all(entry['label'] == 'abstain' for entry in row['sources']['reviews']))

    def test_unanchored_component_is_not_ranked(self):
        component = self.result['components']['i121']
        self.assertFalse(component['expert_anchored'])
        self.assertEqual(component['ranking'], [])
        self.assertEqual(component['status'], 'unidentified_no_anchor')
        statuses = {row['status'] for row in self.result['items']
                    if row['component'] == 'i121'}
        self.assertEqual(statuses, {'unverified', 'abstained'})
        for row in self.result['items']:
            if row['component'] == 'i121':
                self.assertIsNone(row['recommendation'])
                self.assertIsNone(row['leading_probability'])
                self.assertTrue(row['needs_expert'])

    def test_reliability_never_uses_majority_agreement_as_truth(self):
        # s1 在交叠资料上与 s0 大量不一致，但没有专家锚点的资料不参与可靠度。
        for row in self.result['supplier_reliability']:
            for anchor in row['confusion']['cells']:
                self.assertIn('expert_label', anchor)
            self.assertGreater(row['anchors'], 0)

    def test_consensus_probability_is_not_labelled_accuracy(self):
        for row in self.result['items']:
            if row['status'] == 'supported_single_source':
                self.assertIn('单源结论', row['interpretation'])
            if row['status'] == 'low_confidence':
                self.assertIn('不应直接对外称为共识结论', row['interpretation'])
                self.assertIsNone(row['recommendation'])
                self.assertIsNotNone(row['leading_label'])
            self.assertIn('interpretation', row)
        self.assertIn('不是无条件准确率',
                      self.result['interpretation']['consensus_probability'])
        self.assertIn('专业结论',
                      self.result['interpretation']['expert_anchor'])

    def test_mapping_shift_is_flagged_not_ranked(self):
        s1 = supplier(self.result, 's1')
        self.assertEqual(s1['support'], 'mapping_review_required')
        self.assertTrue(s1['confusion']['mapping_diagnosis']['suspect'])
        component = self.result['components']['i0']
        # s1 被排除后只剩一个可标准化供应商，不能形成高低排序。
        self.assertEqual(component['status'], 'insufficient_comparable_suppliers')
        self.assertFalse(component['comparable'])
        self.assertEqual(component['ranking'], [])
        excluded = {row['supplier'] for row in component['excluded_from_ranking']}
        self.assertEqual(excluded, {'s1'})
        estimates = {row['supplier']: row for row in component['supplier_estimates']}
        self.assertTrue(estimates['s0']['rankable_in_group'])
        self.assertIn('s0', estimates)

    def test_expert_revision_changes_reliability_but_does_not_rewrite_votes(self):
        row = item(self.result, 'i4')
        self.assertEqual(row['anchor']['expert_label'], 'c2')
        self.assertEqual(row['anchor']['expert_revision'], 2)
        self.assertFalse(row['anchor']['not_used_in_consensus'] is False)
        sensitivity = self.result['revision_sensitivity']['rows']
        self.assertTrue(any(entry['changed'] for entry in sensitivity))

    def test_expert_queue_is_evidence_prioritised_and_respects_slots(self):
        queue = analyse(self.dataset, {'expert_slots': 5})['expert_queue']
        self.assertEqual(len(queue['selected']), 5)
        selected = queue['candidates'][:5]
        self.assertTrue(all(row['priority'] == 'evidence_gap' for row in selected))
        self.assertEqual(queue['candidate_count'],
                         len(queue['selected']) + len(queue['not_selected']))
        self.assertTrue(all(row['needs_expert'] for row in self.result['items']
                            if row['status'] in {'contested', 'low_confidence',
                                                 'unverified', 'abstained', 'no_review'}))
        priorities = [row['priority'] for row in queue['candidates']]
        self.assertGreater(priorities.index('single_source_unvalidated'),
                           priorities.index('evidence_gap'))

    def test_stratum_composition_is_standardised_across_suppliers(self):
        # 两家供应商在三个资料层都有锚点，但锚点数量和层分布不同。
        dataset = seed_dataset()
        dataset['reviews'] = [row for row in dataset['reviews'] if row['supplier'] != 's2']
        dataset['assignments'] = [row for row in dataset['assignments'] if row['supplier'] != 's2']
        dataset['items'] = [row for row in dataset['items'] if int(row['item_id'][1:]) < 120]
        dataset['expert_versions'] = [row for row in dataset['expert_versions']
                                      if int(row['item_id'][1:]) < 120]
        # 让 s1 在锚点上与专家一致，避免触发映射疑点。
        expert = {row['item_id']: row['expert_label'] for row in dataset['expert_versions']}
        for row in dataset['reviews']:
            if row['supplier'] == 's1' and row['item_id'] in expert:
                row['label'] = expert[row['item_id']]
        result = analyse(dataset)
        component = result['components']['i0']
        self.assertEqual(component['status'], 'comparable')
        self.assertEqual(len(component['ranking']), 2)
        for row in component['ranking']:
            self.assertAlmostEqual(sum(part['weight'] for part in row['parts']), 1.0, places=5)
            self.assertEqual(row['status'], 'complete')
            self.assertEqual(row['class_status'], 'complete')
            self.assertAlmostEqual(sum(part['weight'] for part in row['class_parts']),
                                   1.0, places=5)
            self.assertIsNotNone(row['composite_reliability'])
        self.assertEqual([row['rank'] for row in component['ranking']], [1, 2])

    def test_empty_and_malformed_inputs_are_reported(self):
        empty = {'items': [], 'assignments': [], 'reviews': [], 'expert_versions': []}
        result = analyse(empty)
        self.assertEqual(result['summary']['items'], 0)
        self.assertEqual(result['expert_queue']['selected'], [])

        malformed = copy.deepcopy(self.dataset)
        malformed['reviews'].append({'item_id': 'missing', 'supplier': 's0',
                                     'round': 1, 'label': 'c0'})
        malformed['reviews'].append({'item_id': 'i0', 'supplier': 's0',
                                     'round': 2, 'label': 'not-a-category'})
        result = analyse(malformed)
        self.assertTrue(result['issues'])
        self.assertTrue(any(issue['table'] == 'reviews' for issue in result['issues']))

    def test_invalid_labels_are_isolated_and_queued_first(self):
        dataset = {'items': [{'item_id': 'x', 'stratum': 's', 'categories': ['c0', 'c1']}],
                   'assignments': [{'item_id': 'x', 'supplier': 'a', 'probability': 0.5}],
                   'reviews': [{'item_id': 'x', 'supplier': 'a', 'round': 1,
                                'label': 'c9', 'revision': 1}],
                   'expert_versions': []}
        result = analyse(dataset)
        row = result['items'][0]
        self.assertEqual(row['status'], 'invalid_review_records')
        self.assertIsNone(row['recommendation'])
        self.assertEqual(result['expert_queue']['candidates'][0]['priority'], 'data_quality')
        self.assertTrue(result['issues'])

    def test_single_source_recommendation_is_marked_medium(self):
        row = item(self.result, 'i8')
        self.assertEqual(row['status'], 'supported_single_source')
        self.assertEqual(row['confidence'], 'medium')
        self.assertEqual(row['recommendation'], 'c2')
        self.assertIn('单源结论', row['interpretation'])

    def test_unknown_options_are_rejected(self):
        with self.assertRaises(ValueError):
            analyse(self.dataset, {'made_up': 1})
        with self.assertRaises(ValueError):
            analyse(self.dataset, {'expert_slots': -1})
        with self.assertRaises(ValueError):
            analyse(self.dataset, ['not', 'an', 'object'])

    def test_single_category_item_is_definitional_not_statistical(self):
        dataset = {'items': [{'item_id': 'x', 'stratum': 's', 'categories': ['c0']}],
                   'assignments': [{'item_id': 'x', 'supplier': 'a', 'probability': 0.5}],
                   'reviews': [], 'expert_versions': []}
        result = analyse(dataset)
        row = result['items'][0]
        self.assertEqual(row['status'], 'single_category')
        self.assertEqual(row['recommendation'], 'c0')
        self.assertFalse(row['needs_expert'])
        self.assertEqual(result['expert_queue']['candidate_count'], 0)

    def test_analysis_does_not_mutate_snapshot(self):
        before = copy.deepcopy(self.dataset)
        analyse(self.dataset)
        self.assertEqual(self.dataset, before)


class VersioningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = Service('data/source.json',
                               Path(self.temporary.name) / 'state.sqlite3')

    def run_version(self, request_id, **extra):
        request = {'request_id': request_id, 'operation': 'category_consensus',
                   'as_of': AS_OF, **extra}
        return self.service.run(request)

    def test_new_version_does_not_overwrite_discussed_version(self):
        first = self.run_version('renewal-v1')
        second = self.run_version('renewal-v2', supersedes='renewal-v1')
        self.assertEqual(first['superseded_by'], None)
        old = self.service.repository.analysis('renewal-v1')
        self.assertEqual(old['summary'], first['summary'])
        self.assertEqual(old['superseded_by'], 'renewal-v2')
        self.assertEqual(second['supersedes'], 'renewal-v1')
        self.assertEqual(second['lineage'], ['renewal-v1'])

    def test_lineage_chain_is_ordered_and_complete(self):
        self.run_version('v1')
        self.run_version('v2', supersedes='v1')
        self.run_version('v3', supersedes='v2')
        for request_id in ('v1', 'v2', 'v3'):
            self.assertEqual(self.service.repository.lineage(request_id)['chain'],
                             ['v1', 'v2', 'v3'])
        self.assertEqual(self.service.repository.current(), ['v3'])

    def test_supersedes_missing_version_is_rejected(self):
        with self.assertRaises(KeyError):
            self.run_version('v1', supersedes='does-not-exist')

    def test_supersedes_cycle_is_rejected(self):
        self.run_version('v1')
        self.run_version('v2', supersedes='v1')
        with self.assertRaises(ValueError):
            self.run_version('v1', supersedes='v2')

    def test_same_request_id_with_different_supersedes_conflicts(self):
        self.run_version('v1')
        self.run_version('v2', supersedes='v1')
        with self.assertRaises(Conflict):
            self.run_version('v2')

    def test_recomputation_with_new_reviews_updates_consensus(self):
        first = self.run_version('v1')
        updated = copy.deepcopy(seed_dataset())
        # 给 s2 的一条无锚点资料补专家结论，下一版应能识别该分组。
        updated['expert_versions'].append({'item_id': 'i121', 'expert_label': 'c1',
                                           'revision': 1,
                                           'received_at': '2026-09-25T00:00:00Z'})
        source = Path(self.temporary.name) / 'source.json'
        source.write_text(__import__('json').dumps(updated, ensure_ascii=False))
        service = Service(source, Path(self.temporary.name) / 'state.sqlite3')
        second = service.run({'request_id': 'v2', 'operation': 'category_consensus',
                              'as_of': AS_OF, 'supersedes': 'v1'})
        self.assertGreaterEqual(second['summary']['expert_anchor_items'],
                                first['summary']['expert_anchor_items'])
        self.assertGreaterEqual(second['components']['i121']['anchor_counts']['s2'], 1)


if __name__ == '__main__':
    unittest.main()
