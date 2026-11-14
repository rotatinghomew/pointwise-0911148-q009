import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Conflict(ValueError):
    pass


class Repository:
    def __init__(self, filename):
        self.filename = str(filename)
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS datasets(
                    digest TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analyses(
                    request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
                    dataset_digest TEXT NOT NULL REFERENCES datasets(digest),
                    operation TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions(
                    action_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.filename, timeout=10)
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def remember(self, request_id, request, dataset, result):
        result = json.loads(canonical(result))
        dataset_digest = digest(dataset)
        request_digest = digest(request)
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute(
                'SELECT request_digest,payload FROM analyses WHERE request_id=?',
                (request_id,),
            ).fetchone()
            if previous:
                if previous[0] != request_digest:
                    raise Conflict('该请求编号已对应其他分析输入')
                return json.loads(previous[1])
            connection.execute('INSERT OR IGNORE INTO datasets VALUES(?,?)',
                               (dataset_digest, canonical(dataset)))
            result.update(request_id=request_id, dataset_digest=dataset_digest,
                          operation=request['operation'], as_of=request['as_of'])
            connection.execute('INSERT INTO analyses VALUES(?,?,?,?,?)',
                               (request_id, request_digest, dataset_digest,
                                request['operation'], canonical(result)))
        return result

    def previous(self, request_id, request):
        with self.connect() as connection:
            record = connection.execute(
                'SELECT request_digest,payload FROM analyses WHERE request_id=?',
                (request_id,),
            ).fetchone()
        if record:
            if record[0] != digest(request):
                raise Conflict('该请求编号已对应其他分析输入')
            return json.loads(record[1])
        return None

    def analysis(self, request_id):
        with self.connect() as connection:
            record = connection.execute('SELECT payload FROM analyses WHERE request_id=?',
                                        (request_id,)).fetchone()
        if record is None:
            raise KeyError('未找到分析记录')
        return json.loads(record[0])

    def dataset(self, dataset_digest):
        with self.connect() as connection:
            record = connection.execute('SELECT payload FROM datasets WHERE digest=?',
                                        (dataset_digest,)).fetchone()
        if record is None:
            raise KeyError('未找到数据版本')
        return json.loads(record[0])

    def action(self, kind, payload):
        with self.connect() as connection:
            cursor = connection.execute('INSERT INTO actions(kind,payload) VALUES(?,?)',
                                        (kind, canonical(payload)))
            return {'action_id': cursor.lastrowid, 'kind': kind, **payload}
