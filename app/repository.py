import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
                CREATE TABLE IF NOT EXISTS lineage(
                    request_id TEXT PRIMARY KEY REFERENCES analyses(request_id),
                    supersedes TEXT,
                    superseded_by TEXT,
                    created_at TEXT NOT NULL
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

    def remember(self, request_id, request, dataset, result, supersedes=None):
        """保存一次分析。历史版本永不覆盖；新版本通过 supersedes 建立版本链。"""
        result = json.loads(canonical(result))
        dataset_digest = digest(dataset)
        request_digest = digest(request)
        created_at = datetime.now(timezone.utc).isoformat()
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
            if supersedes:
                target = connection.execute(
                    'SELECT 1 FROM analyses WHERE request_id=?', (supersedes,),
                ).fetchone()
                if target is None:
                    raise KeyError('要取代的分析版本不存在')
            connection.execute('INSERT OR IGNORE INTO datasets VALUES(?,?)',
                               (dataset_digest, canonical(dataset)))
            lineage = []
            if supersedes:
                lineage = self._lineage_chain(connection, supersedes)
                if request_id in lineage:
                    raise ValueError('版本链不能循环引用')
            result.update(request_id=request_id, dataset_digest=dataset_digest,
                          operation=request['operation'], as_of=request['as_of'],
                          supersedes=supersedes, superseded_by=None,
                          lineage=(lineage if supersedes else []))
            connection.execute('INSERT INTO analyses VALUES(?,?,?,?,?)',
                               (request_id, request_digest, dataset_digest,
                                request['operation'], canonical(result)))
            connection.execute('INSERT INTO lineage VALUES(?,?,?,?)',
                               (request_id, supersedes, None, created_at))
            if supersedes:
                # 已用于续约讨论的版本内容保持不可变，只在新版本和 lineage 表里记录接续关系。
                connection.execute('UPDATE lineage SET superseded_by=? WHERE request_id=?',
                                   (request_id, supersedes))
        return result

    def _lineage_chain(self, connection, request_id):
        chain, seen = [], set()
        current = request_id
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            row = connection.execute('SELECT supersedes FROM lineage WHERE request_id=?',
                                     (current,)).fetchone()
            current = row[0] if row else None
        return list(reversed(chain))

    def lineage(self, request_id):
        with self.connect() as connection:
            row = connection.execute(
                'SELECT supersedes,superseded_by,created_at FROM lineage WHERE request_id=?',
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError('未找到分析记录')
            ancestors = self._lineage_chain(connection, request_id)
            descendants = []
            current = request_id
            while True:
                nxt = connection.execute(
                    'SELECT superseded_by FROM lineage WHERE request_id=?', (current,),
                ).fetchone()
                if not nxt or not nxt[0]:
                    break
                current = nxt[0]
                descendants.append(current)
        return {'request_id': request_id, 'supersedes': row[0],
                'superseded_by': row[1], 'created_at': row[2],
                'ancestors': ancestors, 'descendants': descendants,
                'chain': ancestors + descendants}

    def current(self):
        """返回尚未被后续版本接续、且有版本链关系的分析编号。"""
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT request_id FROM lineage WHERE superseded_by IS NULL'
            ).fetchall()
        return [row[0] for row in rows]

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
            payload = json.loads(record[0])
            row = connection.execute(
                'SELECT supersedes,superseded_by FROM lineage WHERE request_id=?',
                (request_id,)).fetchone()
        if row is not None:
            payload['supersedes'] = row[0]
            payload['superseded_by'] = row[1]
        return payload

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
