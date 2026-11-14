from collections import Counter
from pathlib import Path

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

OPERATIONS = {'assignment_overlap': assignment_overlap}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
