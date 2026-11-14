import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.contracts import visible
from app.domain import OPERATIONS, seed_dataset
from app.repository import digest
from app.service import Service


class BusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.operation = next(iter(OPERATIONS))

    def run_check(self):
        return OPERATIONS[self.operation](self.data, {})

    def test_reviews_use_declared_category_and_revision_identity(self):
        categories = {row['item_id']: set(row['categories']) for row in self.data['items']}
        self.assertTrue(all(row['label'] == 'abstain' or row['label'] in categories[row['item_id']] for row in self.data['reviews']))
        result = self.run_check()
        self.assertLess(result['reviews'], len(self.data['reviews']))
        self.assertGreater(result['abstentions'], 0)

    def test_unanchored_supplier_component_exists(self):
        items = {row['item_id'] for row in self.data['reviews'] if row['supplier'] == 's2'}
        experts = {row['item_id'] for row in self.data['expert_versions']}
        self.assertTrue(items)
        self.assertFalse(items & experts)
        self.assertTrue(all(row['supplier'] == 's2' for row in self.data['reviews'] if row['item_id'] in items))


    def test_fixture_timestamps_do_not_arrive_before_events(self):
        from app.contracts import instant
        for records in self.data.values():
            for row in records:
                if 'received_at' not in row:
                    continue
                for key in ('event_at', 'assigned_at', 'signed_at', 'paid_at', 'issued_at'):
                    if row.get(key):
                        self.assertLessEqual(instant(row[key]), instant(row['received_at']))

    def test_seed_and_live_source_have_one_contract(self):
        from app.contracts import load
        self.assertEqual(self.data, load('data/source.json'))
