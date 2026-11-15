from collections import Counter
from pathlib import Path

from app.consensus import analyse, summarise
from app.contracts import latest, load
from app.repository import digest

TABLES = ['items', 'assignments', 'reviews', 'expert_versions']

def assignment_overlap(snapshot, options):
    reviews = latest(snapshot['reviews'], ['item_id', 'supplier', 'round'])
    experts = latest(snapshot['expert_versions'], ['item_id'])
    counts = Counter(row['supplier'] for row in snapshot['assignments'])
    seen = {}
    for review in reviews:
        seen.setdefault(review['item_id'], set()).add(review['supplier'])
    return {'items': len(snapshot['items']), 'assignments': len(snapshot['assignments']),
            'reviews': len(reviews), 'expert_items': len(experts), 'suppliers': dict(counts),
            'overlapped_items': sum(len(suppliers) > 1 for suppliers in seen.values()),
            'abstentions': sum(row['label'] == 'abstain' for row in reviews)}

def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

def category_consensus(snapshot, options):
    """类别共识与供应商可靠度分析；完整明细随请求留存。"""
    return analyse(snapshot, options)


def category_consensus_summary(snapshot, options):
    """同一分析的续约讨论精简视图，便于人工复核。"""
    return summarise(analyse(snapshot, options))


OPERATIONS = {'assignment_overlap': assignment_overlap,
              'category_consensus': category_consensus,
              'category_consensus_summary': category_consensus_summary}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
