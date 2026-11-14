import copy
import json
from datetime import datetime, timezone


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load(filename):
    with open(filename, encoding='utf-8') as stream:
        return json.load(stream)


def visible(dataset, as_of):
    cutoff = instant(as_of)
    result = {}
    for table, records in dataset.items():
        if not isinstance(records, list):
            raise ValueError('数据表须为记录数组')
        result[table] = [copy.deepcopy(record) for record in records
                         if 'received_at' not in record or instant(record['received_at']) <= cutoff]
    return result


def latest(records, keys):
    selected = {}
    for record in records:
        identity = tuple(record[key] for key in keys)
        previous = selected.get(identity)
        position = (record.get('revision', 0), record.get('received_at', ''))
        if previous is None or position > (previous.get('revision', 0), previous.get('received_at', '')):
            selected[identity] = record
    return list(selected.values())


def require_tables(dataset, tables):
    absent = set(tables) - dataset.keys()
    if absent:
        raise ValueError('缺少业务数据表：' + ','.join(sorted(absent)))
