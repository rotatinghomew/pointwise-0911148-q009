OPERATION = 'assignment_overlap'

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.contracts import load, visible
from app.httpd import create_server
from app.repository import Conflict, digest
from app.service import Service


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source.json'
        self.source.write_text(Path('data/source.json').read_text())
        self.service = Service(self.source, Path(self.temporary.name) / 'state.sqlite3')
        self.request = {'request_id': 'check', 'operation': OPERATION,
                        'as_of': '2026-09-30T23:59:59Z'}

    def test_persisted_input_matches_result(self):
        result = self.service.run(self.request)
        saved = self.service.repository.dataset(result['dataset_digest'])
        self.assertEqual(result['dataset_digest'], digest(saved))
        self.assertEqual(result, self.service.repository.analysis('check'))

    def test_idempotent_request(self):
        self.assertEqual(self.service.run(self.request), self.service.run(self.request))

    def test_conflicting_request_is_rejected(self):
        self.service.run(self.request)
        with self.assertRaises(Conflict):
            self.service.run({**self.request, 'as_of': '2026-08-01'})

    def test_unknown_operation(self):
        with self.assertRaises(ValueError):
            self.service.run({**self.request, 'operation': 'unknown'})

    def test_cutoff_filters_late_records(self):
        dataset = load(self.source)
        table = next(iter(dataset))
        extra = copy.deepcopy(dataset[table][0])
        extra['received_at'] = '2099-01-01T00:00:00Z'
        dataset[table].append(extra)
        self.assertNotIn(extra, visible(dataset, self.request['as_of'])[table])

    def test_old_result_survives_source_replacement(self):
        previous = self.service.run(self.request)
        self.source.write_text('{}')
        self.assertEqual(previous, self.service.run(self.request))
        self.assertEqual(previous, self.service.repository.analysis('check'))

    def test_concurrent_replay(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.service.run(self.request), range(8)))
        self.assertTrue(all(result == results[0] for result in results))

    def test_http_roundtrip(self):
        server = create_server(self.service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with urlopen(base + '/health') as response:
                self.assertEqual(json.load(response)['status'], 'ok')
            request = Request(base + '/v1/analyses', data=json.dumps(self.request).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                result = json.load(response)
            with urlopen(base + '/v1/analyses/check') as response:
                self.assertEqual(result, json.load(response))
            with urlopen(base + '/v1/analyses/check/lineage') as response:
                self.assertEqual(json.load(response)['request_id'], 'check')
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base + '/v1/analyses', data=b'{'))
            self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
