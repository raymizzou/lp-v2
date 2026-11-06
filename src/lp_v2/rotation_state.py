"""Market-scoped journal paths and strict identity checks for rotation history."""
from pathlib import Path
from decimal import Decimal
import re


def market_path(state_dir, condition_id, *, create=False):
    if not isinstance(condition_id, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', condition_id):
        raise ValueError('invalid market directory identity')
    root = Path(state_dir)
    for path in (root, root/'markets', root/'markets'/condition_id):
        if path.is_symlink():
            raise ValueError('market state symlink refused')
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and not path.is_dir():
            raise ValueError('market state is not a directory')
    path = root/'markets'/condition_id/'journal.sqlite'
    for suffix in ('', '-wal', '-shm', '-journal'):
        if path.with_name(path.name+suffix).is_symlink():
            raise ValueError('journal symlink refused')
    return path


def open_market_journal(state_dir, identity):
    from lp_v2.journal import Journal
    path = market_path(state_dir, identity['condition_id'], create=True)
    journal = Journal(path)
    try:
        known = journal.events('identity')
        if known and any(value != identity for _, value in known):
            raise ValueError('state belongs to a different wallet or market')
        if not known:
            journal.append('identity', identity)
        path.chmod(0o600)
        return journal
    except BaseException:
        journal.close()
        raise


def read_market_identity(state_dir, condition_id):
    import sqlite3
    from lp_v2.journal import decode
    path = market_path(state_dir, condition_id)
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        rows = [decode(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='identity' ORDER BY seq")]
        if (not rows or any(r != rows[0] for r in rows) or rows[0]['condition_id'] != condition_id
                or len(rows[0]['token_ids']) != 2 or len(set(rows[0]['token_ids'])) != 2
                or not all(isinstance(t,str) and t for t in rows[0]['token_ids'])):
            raise ValueError('market journal identity unavailable')
        return rows[0]
    finally:
        db.close()


def validate_histories(state_dir, owner, *, candidate=None):
    """Fail closed before generating IDs, including short-prefix collisions."""
    import sqlite3
    from lp_v2.journal import decode
    markets = Path(state_dir)/'markets'
    if markets.is_symlink():
        raise ValueError('market state symlink refused')
    prefixes, identifiers, identities = {}, set(), []
    paths = list(markets.iterdir()) if markets.exists() else []
    cids = [path.name for path in paths]
    if candidate is not None:
        cids.append(candidate)
    for cid in dict.fromkeys(cids):
        path = market_path(state_dir,cid)
        prefix = cid[:8]
        if prefix in prefixes and prefixes[prefix] != cid:
            raise ValueError('market prefix collision')
        prefixes[prefix] = cid
        if cid == candidate and not path.exists():
            continue
        identity = read_market_identity(state_dir,cid)
        if identity['owner'] != owner:
            raise ValueError('historical market owner mismatch')
        identities.append(identity)
        db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            for kind,payload in db.execute("SELECT kind,payload FROM events WHERE kind IN ('intent','merge_intent') ORDER BY seq"):
                record = decode(payload)['request']
                key = record.request_id if kind == 'intent' else record.merge_id
                if record.owner != owner or (record.quote.condition_id if kind == 'intent' else record.condition_id) != cid:
                    raise ValueError('historical request identity mismatch')
                if key in identifiers:
                    raise ValueError('historical request id collision')
                identifiers.add(key)
        finally:
            db.close()

    return tuple(identities)


def active_market(state_dir, seed, owner):
    """Resolve only validated durable market identities; never use raw path components."""
    import sqlite3
    from lp_v2.journal import decode
    root = Path(state_dir)
    rotation = root/'rotation.sqlite'
    if not rotation.exists() and not rotation.is_symlink():
        return seed
    if rotation.is_symlink():
        raise ValueError('rotation symlink refused')
    from lp_v2.cli import owner_file
    with owner_file(rotation):
        pass
    db = sqlite3.connect(rotation.as_uri()+'?mode=ro',uri=True)
    try:
        identities = [decode(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='rotation_identity'")]
        if not identities or any(r['owner'] != owner or r['seed_market'] != seed for r in identities):
            raise ValueError('rotation identity mismatch')
        row = db.execute("SELECT payload FROM events WHERE kind='rotation' ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError('rotation phase unavailable')
        phase = decode(row[0])
        if phase['phase'] not in ('running','exiting','cleared','revalidating'):
            raise ValueError('rotation phase invalid')
        cid = phase['new_market'] if phase['phase'] == 'running' else phase['old_market']
        identity = read_market_identity(root,cid)
        if identity['owner'] != owner:
            raise ValueError('active owner mismatch')
        return cid
    finally:
        db.close()


def control_market_profile(profile):
    from copy import deepcopy
    result = deepcopy(profile)
    owner = 'lp-v2:'+profile['identity']['wallet_address'].lower()
    result['trading']['market_condition_id'] = active_market(profile['files']['state_dir'],
        profile['trading']['market_condition_id'],owner)
    return result


def carry_legacy_journal(state_dir, identity):
    """Copy a verified legacy database once; retain its original bytes and history."""
    import sqlite3
    from lp_v2.cli import owner_file
    from lp_v2.journal import decode
    source = Path(state_dir)/'journal.sqlite'
    if not source.exists() and not source.is_symlink():
        if any(source.with_name(source.name+s).exists() for s in ('-wal','-shm','-journal')):
            raise ValueError('orphan legacy journal history')
        return
    with owner_file(source):
        pass
    for suffix in ('-wal','-shm','-journal'):
        sidecar = source.with_name(source.name+suffix)
        if sidecar.exists() or sidecar.is_symlink():
            with owner_file(sidecar) as stream:
                if suffix != '-shm' and __import__('os').fstat(stream.fileno()).st_size:
                    raise ValueError('unsettled legacy history')
    old = sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        prefix = list(old.execute('SELECT kind,payload FROM events ORDER BY seq'))
        identities = [decode(p) for k,p in prefix if k == 'identity']
        if not identities or any(record != identity for record in identities):
            raise ValueError('legacy state identity mismatch')
        target = market_path(state_dir,identity['condition_id'],create=True)
        if target.exists():
            new = sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
            try:
                if list(new.execute('SELECT kind,payload FROM events ORDER BY seq LIMIT ?', (len(prefix),))) != prefix:
                    raise ValueError('legacy history diverges from market journal')
            finally:
                new.close()
        else:
            # sqlite backup retains ordered facts without importing foreign risk.
            new = sqlite3.connect(target)
            try:
                old.backup(new)
            finally:
                new.close()
            target.chmod(0o600)
    finally:
        old.close()


def read_control_window(config, *, on_error=None):
    """Only a bound persisted on action grants automatic new-market entry."""
    from lp_v2.live_control.profiles import read_json, timestamp
    from lp_v2.live_control.identity import same_identity
    import re
    state = Path(config.state_dir)
    path = state/'live-control-run.json'
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        record = read_json(path)
        profile = record['profile']
        process = read_json(state/'process.json')
        credentials = config.credentials
        identity = dict(wallet_address=credentials.wallet_address,signer_address=credentials.signer_address,
                        signature_type=credentials.signature_type)
        action, end = record['action_id'], timestamp(record['window_end'])
        started = timestamp(record['started_at'])
        if (not re.fullmatch('[0-9a-f]{32}',action) or not re.fullmatch('[0-9a-f]{32}',process['run_id'])
                or record['run_id'] not in (None,process['run_id']) or not same_identity(profile['identity'],identity)
                or profile['trading']['market_condition_id'] != config.market
                or Decimal(profile['trading']['capital_usdc']) != config.capital_usdc
                or profile['release']['expected_sha'] != config.code_sha or not started < end
                or started > config.started_at
                or Path(profile['files']['state_dir']).resolve() != state.resolve()
                or Path(profile['files']['signing_key']).resolve() != Path(config.private_key_file).resolve()
                or Path(profile['files']['relayer_credentials']).resolve() != Path(config.relayer_credentials_file).resolve()):
            raise ValueError('control binding')
        persisted = read_json(profile['files']['l2_credentials'])
        if any(persisted.get(key) != getattr(credentials,key) for key in ('key','secret','passphrase','signature_type')):
            raise ValueError('credential binding')
        if not same_identity({key:persisted.get(key) for key in identity},identity):
            raise ValueError('credential identity binding')
        if 'lease_id' in process:
            import os,fcntl
            from lp_v2.cli import owner_file
            from lp_v2.live_process import process_identity
            from lp_v2.live_control.profiles import fingerprint,atomic_json
            if (process['pid']!=os.getpid() or process_identity(process['pid'])!=process['identity']
                    or not re.fullmatch('[0-9a-f]{32}',process['lease_id'])
                    or process.get('action_id')!=action or process.get('profile_sha256')!=fingerprint(profile)):
                raise ValueError('physical executor binding')
            with owner_file(state/'executor.lock') as lock:
                try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:raise ValueError('exclusive executor lease missing')
            bound=physical_binding(process)
            prior=record.get('process_binding')
            if prior is not None and prior!=bound and (process.get('recovered_from')!=prior or prior.get('run_id')!=process['run_id']):
                raise ValueError('physical lease provenance changed')
            if prior!=bound or record['run_id'] is None:
                atomic_json(state/'live-control-run.json',dict(record,run_id=process['run_id'],process_binding=bound))
        return action,end
    except Exception as error:
        if on_error is not None:
            on_error(error)
        raise ValueError('persisted on authorization unavailable or mismatched') from None


def journal_start_sequences(profile):
    """Sequence numbers belong to one market database, never the seed's neighbor."""
    import sqlite3
    owner = 'lp-v2:'+profile['identity']['wallet_address'].lower()
    identities = validate_histories(profile['files']['state_dir'],owner)
    result = {}
    for identity in identities:
        path = market_path(profile['files']['state_dir'],identity['condition_id'])
        from lp_v2.cli import owner_file
        with owner_file(path):
            pass
        db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            result[identity['condition_id']] = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE kind='occupation'").fetchone()[0]
        finally:
            db.close()
    return result


def run_journal_sequence(record, market):
    if 'journal_seqs' in record:
        values = record['journal_seqs']
        if not isinstance(values,dict):
            raise ValueError('journal sequence map unavailable')
        value = values.get(market,0)
    else:
        value = record['journal_seq']
    if type(value) is not int or value < 0:
        raise ValueError('journal sequence invalid')
    return value


def start_verification_passed(config, action_id, window_end, run_id, now, *, on_error=None):
    """A durable proof for this exact on/run unlocks rotation, never an elapsed delay."""
    from lp_v2.live_control.profiles import read_json,timestamp
    try:
        action,end = read_control_window(config, on_error=on_error)
        record = read_json(Path(config.state_dir)/'live-control-run.json')
        process = read_json(Path(config.state_dir)/'process.json')
        proof = record['startup_verified']
        moments = [tuple(timestamp(value) for value in row) for row in proof['moments']]
        verified = timestamp(proof['verified_at'])
        return (action == action_id and end == window_end and process['run_id'] == record['run_id'] == run_id
            and proof['action_id'] == action_id and proof['run_id'] == run_id
            and timestamp(proof['window_end']) == window_end and proof['seed_market'] == config.market
            and type(proof['verified_rounds']) is int and proof['verified_rounds'] == 2
            and len(moments) == 2 and all(len(row) == 3 for row in moments)
            and all(value > timestamp(record['started_at']) and value <= verified
                    for row in moments for value in row)
            and all(new > old for new,old in zip(moments[1],moments[0])) and verified <= now)
    except Exception as error:
        if on_error is not None:
            on_error(error)
        return False


def physical_binding(process):
    return {key:process[key] for key in ('run_id','lease_id','pid','identity','action_id','profile_sha256')}


def recoverable_run(state, previous):
    """Reuse only a bound logical on; a fresh exclusive lease always stays physical."""
    import sqlite3
    from lp_v2.live_control.profiles import read_json,fingerprint,timestamp
    from lp_v2.live_process import process_identity
    from lp_v2.journal import decode
    from lp_v2.cli import owner_file
    try:
        control=read_json(Path(state)/'live-control-run.json')
        bound=physical_binding(previous)
        if (control['run_id']!=previous['run_id'] or control.get('process_binding')!=bound
                or control['action_id']!=previous['action_id']
                or fingerprint(control['profile'])!=previous['profile_sha256']
                or process_identity(previous['pid'])==previous['identity']):
            return None
        path=Path(state)/'rotation.sqlite'
        with owner_file(path):pass
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            phase=decode(db.execute("SELECT payload FROM events WHERE kind='rotation' ORDER BY seq DESC LIMIT 1").fetchone()[0])
            identity=decode(db.execute("SELECT payload FROM events WHERE kind='rotation_identity' ORDER BY seq LIMIT 1").fetchone()[0])
        finally:db.close()
        account=control['profile']['identity']
        if (phase['action_id']!=control['action_id'] or phase['window_end']!=timestamp(control['window_end'])
                or identity['seed_market']!=control['profile']['trading']['market_condition_id']
                or identity['owner']!='lp-v2:'+account['wallet_address'].lower()
                or identity['wallet'].lower()!=account['wallet_address'].lower()
                or identity['signer'].lower()!=account['signer_address'].lower()):
            return None
        return bound
    except Exception:
        return None
