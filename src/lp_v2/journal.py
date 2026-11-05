"""Append-only, fsynced JSON facts. Decoding is restricted to our value types."""
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import json
import sqlite3

from lp_v2 import models

_TYPES = {name: value for name, value in vars(models).items()
          if isinstance(value, type) and (is_dataclass(value) or issubclass(value, Enum))}


def _pack(value):
    if isinstance(value, Enum):
        return {'type': type(value).__name__, 'value': value.value}
    if is_dataclass(value):
        return {'type': type(value).__name__, 'fields': {f.name: _pack(getattr(value, f.name)) for f in fields(value) if f.init}}
    if isinstance(value, Decimal):
        return {'decimal': str(value)}
    if isinstance(value, datetime):
        return {'datetime': value.isoformat()}
    if isinstance(value, tuple):
        return {'tuple': [_pack(v) for v in value]}
    if isinstance(value, list):
        return [_pack(v) for v in value]
    if isinstance(value, dict):
        return {k: _pack(v) for k, v in value.items()}
    return value


def encode(value):
    return json.dumps(_pack(value), sort_keys=True, allow_nan=False)


def decode(payload):
    def unpack(value):
        if isinstance(value, list):
            return [unpack(v) for v in value]
        if not isinstance(value, dict):
            return value
        if 'decimal' in value:
            return Decimal(value['decimal'])
        if 'datetime' in value:
            return datetime.fromisoformat(value['datetime'])
        if 'tuple' in value:
            return tuple(unpack(v) for v in value['tuple'])
        if 'type' in value:
            cls = _TYPES[value['type']]
            return cls(**{k: unpack(v) for k, v in value['fields'].items()}) if 'fields' in value else cls(value['value'])
        return {k: unpack(v) for k, v in value.items()}
    return unpack(json.loads(payload))


class Journal:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
        self.db.executescript('''
            CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
            CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
        ''')

    def append(self, kind, payload):
        with self.db:
            self.db.execute('INSERT INTO events(kind,payload) VALUES (?,?)', (kind, encode(payload)))

    def events(self, kind=None):
        rows = self.db.execute('SELECT kind,payload FROM events ORDER BY seq')
        return tuple((k, decode(p)) for k, p in rows if kind is None or k == kind)

    def close(self):
        self.db.close()
