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
    def __init__(self, path, *, readonly=False):
        self.path = path
        if readonly:
            from pathlib import Path
            self.db = sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
            self.db.execute('BEGIN')
            return
        self.db = sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS events_kind_seq ON events(kind,seq)')
        self.db.executescript('''
            CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
            CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
        ''')

    def append(self, kind, payload):
        with self.db:
            self.db.execute('INSERT INTO events(kind,payload) VALUES (?,?)', (kind, encode(payload)))

    def day_lower_bound(self, kind, day):
        row=self.db.execute("SELECT seq FROM events WHERE kind=? AND julianday(json_extract(payload,'$.at.datetime'))<julianday(?) ORDER BY seq DESC LIMIT 1",(kind,day.isoformat())).fetchone()
        return row[0]+1 if row else 0

    def last_sequence(self, kind):
        row = self.db.execute('SELECT max(seq) FROM events WHERE kind=?', (kind,)).fetchone()
        return row[0] or 0

    def latest_market_event(self, kind, condition_id, legacy_condition_id):
        row = self.db.execute("""SELECT payload FROM events WHERE kind=? AND
            CASE WHEN json_type(payload,'$.condition_id') IS NULL THEN ?
                 ELSE json_extract(payload,'$.condition_id') END = ?
            ORDER BY seq DESC LIMIT 1""", (kind,legacy_condition_id,condition_id)).fetchone()
        return decode(row[0]) if row else None

    def events(self, kind=None, *, since_seq=0):
        rows = self.db.execute('SELECT kind,payload FROM events WHERE seq>=?'+(' AND kind=?' if kind is not None else '')+' ORDER BY seq', (since_seq,) if kind is None else (since_seq,kind))
        return tuple((k, decode(p)) for k,p in rows)

    def close(self):
        self.db.close()
