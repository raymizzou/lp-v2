from datetime import timedelta
import sqlite3

import pytest

from lp_v2.journal import Journal
from tests.test_live_sdk import AT,CID


def test_narrow_reads_preserve_latest_market_and_legacy_facts(tmp_path):
    path=tmp_path/'journal.sqlite'
    journal=Journal(path)
    other='0x'+'b'*64
    assert journal.last_sequence('income')==0
    assert journal.latest_market_event('income',CID,CID) is None
    explicit=dict(at=AT,condition_id=CID,reward_today='1')
    legacy=dict(at=AT+timedelta(seconds=10),reward_today='2')
    foreign=dict(at=AT+timedelta(seconds=20),condition_id=other,reward_today='3')
    for row in (explicit,legacy,foreign,dict(at=AT,condition_id=None,reward_today='4')):
        journal.append('income',row)
    journal.append('diagnostic',dict(at=AT))
    assert journal.last_sequence('income')==4 and journal.last_sequence('diagnostic')==5
    assert journal.latest_market_event('income',CID,CID)==legacy
    assert journal.latest_market_event('income',other,CID)==foreign
    assert journal.latest_market_event('income',CID,other)==explicit
    with pytest.raises(sqlite3.IntegrityError,match='append only'):
        journal.db.execute("UPDATE events SET kind='changed'")
    reader=Journal(path,readonly=True)
    assert reader.last_sequence('income')==4
    assert reader.latest_market_event('income',CID,CID)==legacy
    assert reader.events()==journal.events()
    with pytest.raises(sqlite3.OperationalError,match='readonly'):
        reader.append('income',explicit)
    reader.close();journal.close()
