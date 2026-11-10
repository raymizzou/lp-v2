"""Serial live-market rotation. Old risk remains owned until independently cleared."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event, Lock

from lp_v2.rotation import evaluate_rotation, rank_candidates
from lp_v2.scoring import SCORE_VERSION
from lp_v2.status_server import build_summary


class _CandidateCheckFailure(ValueError):
    """Expected failed admission, with no change to candidate fallback."""


class RotationRunner:
    def __init__(self, config, current, scanner, *, action_id, window_end,
                 clock=lambda: datetime.now(UTC), account_identity=None, relayer=None, control_run_id=None):
        self.config, self.current, self.scanner = config, current, scanner
        self.clock, self.action_id, self.window_end = clock, action_id, window_end
        self.account_identity, self.relayer = account_identity, relayer
        self.control_run_id = control_run_id
        self._candidate_failures = []
        self._failed_sweeps = 0
        self._next_retry_at = None
        self._admission_proof = None
        self._self_proofs = {}
        self._proof_binding = None
        if control_run_id is not None:
            from lp_v2.rotation_state import runtime_proof_binding
            self._proof_binding = runtime_proof_binding(config,action_id,window_end,control_run_id)
        self.stop = Event()
        from pathlib import Path
        from lp_v2.journal import Journal
        path = Path(config.state_dir)/'rotation.sqlite'
        if path.is_symlink():
            raise ValueError('rotation journal symlink refused')
        self.journal = Journal(path)
        path.chmod(0o600)
        self._runners = {current.condition_id:current}
        from lp_v2.rotation_state import validate_histories
        try:
            historical_identities = validate_histories(config.state_dir,current.sdk.owner)
            self.phase, self.reason = 'running', 'initial_market'
            self.switch_count = 0
            self.entered_count = 0
            self.blocker = None
            self._blocker_phase = None
            self._clearance_blocker = None
            self._entry_blocker = None
            self._exit_observation = None
            self._lock = Lock()
            self.exit_code = None
            identity = dict(owner=current.sdk.owner,wallet=current.sdk.wallet,signer=current.sdk.signer,
                            seed_market=current.condition_id,seed_tokens=current.sdk.token_ids)
            known = self.journal.events('rotation_identity')
            if known and any(record != identity for _,record in known):
                raise ValueError('rotation state identity mismatch')
            if not known:
                self.journal.append('rotation_identity',identity)
            events = self.journal.events('rotation')
            if events:
                last = events[-1][1]
                if last['phase'] not in ('running','exiting','cleared','revalidating'):
                    raise ValueError('unknown rotation phase')
                cid = last['new_market'] if last['phase'] == 'running' else last['old_market']
                if cid != current.condition_id:
                    from lp_v2.rotation_state import read_market_identity
                    active_identity = read_market_identity(config.state_dir,cid)
                    if active_identity['owner'] != current.sdk.owner:
                        raise ValueError('active market owner mismatch')
                    self.current = self._runner_for_market(cid,tuple(active_identity['token_ids']))
                self.phase, self.reason = last['phase'], last['reason']
                self.switch_count = sum(record['phase'] == 'exiting' for _,record in events)
                self.entered_count = sum(record['phase'] == 'running' and record['reason'] == 'entry_verified'
                                         for _,record in events)
            else:
                if action_id is not None and window_end is not None:
                    current.sdk.bind_entry(action_id=action_id,window_end=window_end,activation_id=action_id+':initial')
                self._transition('running', 'initial_market', current.condition_id, current.condition_id)
            for historical in historical_identities:
                self._runner_for_market(historical['condition_id'],tuple(historical['token_ids']))
            if (events and action_id is not None and action_id != events[-1][1]['action_id']):
                # A new confirmed on starts from its checked seed only after every
                # retained market independently proves flat. Restarting the same on
                # continues the persisted active phase instead.
                waiting = next((reason for runner in self._runners.values()
                                if (reason := self._clearance_reason(self.clock(),runner=runner)) is not None),None)
                if waiting is None:
                    old = self.current.condition_id
                    self.current = self._runners[config.market]
                    self.current.stop.clear()
                    self.current.sdk.bind_entry(action_id=action_id,window_end=window_end,activation_id=action_id+':initial')
                    self._transition('running','new_on_seed',old,self.current.condition_id)
                else:
                    self.reason = 'historical_'+waiting
            self._snapshot = self._build(None)
        except BaseException:
            for runner in self._runners.values():
                if runner is not current:
                    runner.sdk.journal.close()
            self.journal.close()
            raise

    @property
    def condition_id(self):
        return self.current.condition_id

    @property
    def sdk(self):
        return self.current.sdk

    @property
    def observations(self):
        return self.current.observations

    def _ranking(self, scan):
        return rank_candidates([r['plan'] for r in scan.rows if r.get('plan') is not None],
                               startup_cap=self.config.capital_usdc) if scan else rank_candidates((), startup_cap=self.config.capital_usdc)

    def _build(self, now):
        scan = self.scanner.latest()
        ranking = self._ranking(scan)
        report = self.current.snapshot()
        stuck_minutes = None
        if self.phase == 'exiting' and self.blocker is not None:
            seconds = ((now or self.clock()) - self.blocker['since']).total_seconds()
            if seconds >= 900:
                stuck_minutes = seconds / 60
        rotation = dict(current=self.current.condition_id if self.phase in ('running','exiting') else None, candidate=ranking.leader.condition_id if ranking.leader else None,
            phase=self.phase, reason=self.reason, blocker=self.blocker, exit_observation=self._exit_observation,
            candidate_failures=self._candidate_failures,next_retry_at=self._next_retry_at, stuck_minutes=stuck_minutes, score_version=SCORE_VERSION,
            candidates=[dict(condition_id=c.condition_id,score=c.score,executable=c.reason is None,reason=c.reason)
                        for c in ranking.candidates],
            score_at=scan.scan['finished_at'] if scan else None, scan=scan.scan if scan else {},
            switch_count=self.switch_count,entered_count=self.entered_count,cancel_count=sum(len(r.sdk.journal.events('cancel_intent')) for r in self._runners.values()))
        report['rotation'] = build_summary(self.config, self.current.ledger.snapshot().capital, rotation=rotation)['rotation']
        return report

    def snapshot(self):
        with self._lock:
            return deepcopy(self._snapshot)

    def step(self, now=None):
        now = self.clock() if now is None else now
        scan = self.scanner.latest()
        ranking = self._ranking(scan)
        def record_round(valid, at):
            observed = self.current._accounting_at
            self._record_self_proof(self.current, observed if valid is True else at,
                valid is True and observed is not None and self.current.sdk.orders_verified())
        def policy(valid, at, *, round_observation=None):
            self._entry_blocker = None
            record_round(valid, at)
            # A seed that has never signed is still a new-market entry on restart.
            # Keep its entry gate without skipping the active runner's step.
            if valid and not self.current.sdk.journal.events('signed'):
                waiting = self._historical_clearance_reason(at)
                if waiting is not None:
                    self.reason = waiting
                    return False
            if valid and self.control_run_id is not None:
                if not self._startup_ready(self.current, at):
                    self.reason = 'waiting_seed_start_verification'
                    return self.phase == 'running' and self._runtime_binding_valid()
            result = evaluate_rotation(ranking,current=self.current.condition_id,phase=self.phase,
                complete=scan.complete if scan else False,current_valid=valid,now=at,window_end=self.window_end)
            if result.action == 'exit':
                if self.phase != 'exiting':
                    self.switch_count += 1
                if self.phase != 'exiting':
                    self._transition('exiting', result.reason, self.current.condition_id, result.target)
                else:
                    self.reason = result.reason
            elif result.action == 'resume':
                self._transition('running', result.reason, self.current.condition_id, self.current.condition_id)
            elif self.phase == 'running':
                self.reason = result.reason
            return self.phase == 'running' and valid is True
        if self.phase in ('running','exiting'):
            self.current.step(now, opening_policy=policy)
        elif self.phase in ('cleared','revalidating'):
            recovery_observation = None
            if self.control_run_id is not None and not self._startup_ready(self.current, now):
                # A restarted cleared executor still owns its journal. Collect real
                # serialized rounds without reopening either old or candidate quotes.
                self.current.stop.clear()
                self.current.exit_code = None
                def recovery_policy(valid, at, *, round_observation=None):
                    nonlocal recovery_observation
                    # Only this actual already-cleared executor cycle may use the
                    # explicit market exclusion; region/UNKNOWN failures never do.
                    facts = round_observation or {}
                    if (valid is False and facts.get('condition_id') == self.current.condition_id
                            and all(facts.get(key) is True for key in ('accounting_verified',
                                'orders_verified','execution_clear','market_ineligible','region_allowed'))):
                        recovery_observation = facts['accounting_at']
                    else:
                        recovery_observation = None
                        record_round(valid, at)
                    return False
                self.current.step(now,opening_policy=recovery_policy)
            waiting = self._clearance_reason(now)
            if recovery_observation is not None:
                self._record_self_proof(self.current,recovery_observation,waiting is None)
            if waiting:
                self._transition('exiting',waiting,self.current.condition_id,None)
            else:
                self._enter(now, self.current.condition_id, self.scanner.latest(complete=True))
        if self.phase == 'exiting':
            self.reason = self._clearance_reason(now)
            if self.reason is None:
                old = self.current.condition_id
                complete_scan=self.scanner.latest(complete=True)
                complete_ranking=self._ranking(complete_scan)
                target = complete_ranking.leader.condition_id if complete_ranking.leader else None
                observation = self._verified_exit
                code = (self.current.finish_verified_exit(observation) if observation is not None
                        else self.current.drain(timeout_s=0))
                if observation is not None:
                    self._exit_observation = observation
                waiting = self._clearance_reason(self.clock())
                if code == 0 and waiting is None:
                    self._transition('cleared', 'old_market_cleared', old, target,scan=complete_scan)
                    self._enter(self.clock(), old, self.scanner.latest(complete=True))
                else:
                    self.reason = waiting or 'waiting_drain'
                    if not self.stop.is_set():
                        self.current.stop.clear()
                        self.current.exit_code = None
        detail = self._clearance_blocker if self.phase == 'exiting' else self._entry_blocker or self.current.blocker
        if detail is None and (self.reason.startswith(('waiting_', 'historical_'))
                or self.reason in ('needs_new_on', 'no_revalidated_candidate', 'no_executable_candidate', 'incomplete_scan', 'window_expired')):
            detail = dict(code=self.reason)
        if detail:
            from lp_v2.wait_policy import wait_blocker
            previous = self.blocker if self._blocker_phase == self.phase else None
            self.blocker = wait_blocker(detail, previous, now)
        else:
            self.blocker = None
        self._blocker_phase = self.phase
        with self._lock:
            self._snapshot = self._build(now)
        return self.snapshot()

    def _clearance_reason(self, now, *, runner=None, require_accounting=False):
        from lp_v2.live_control.host import merge_history_report
        from lp_v2.models import OrderStatus
        runner = runner or self.current
        sdk, ledger = runner.sdk, runner.ledger
        readback = False
        from lp_v2.inventory_evidence import inventory_numbers
        numbers = inventory_numbers(sdk.token_ids)
        safe_to_stop = 'UNKNOWN'
        diagnostic = None
        self._clearance_blocker = None
        self._verified_exit = None
        def blocked(reason, code, *, order_id=None, detail=None):
            self._clearance_blocker = dict(diagnostic or detail or dict(code=code, order_tail=order_id[-8:] if order_id else None),
                                           numbers=numbers, safe_to_stop=safe_to_stop)
            return reason
        try:
            with sdk.read_cycle(now=now):
                runner._reconcile()
                ledger = runner.ledger
                readback = sdk.orders_verified()
            state = ledger.snapshot()
        except Exception as error:
            # Plain ValueError is the existing SDK/ledger domain-guard contract.
            if type(error) is not ValueError:
                from lp_v2.wait_policy import unexpected_error
                diagnostic = unexpected_error(error, 'clearance_reconcile')
            state = ledger.snapshot()
        diagnostic = sdk.unexpected_error or diagnostic
        execution = sdk.execution_risk_verified()
        if state.pending and not execution:
            return blocked('waiting_pending', 'pending_order', detail=sdk.readback_blocker)
        profile = dict(identity=dict(wallet_address=sdk.wallet), trading=dict(market_condition_id=sdk.condition_id))
        expected = {p['request'].merge_id for _, p in sdk.journal.events('merge_intent')}
        if (expected != {r.request.merge_id for r in state.merges}
                or merge_history_report(profile, state.merges)['status'] != 'PASS'):
            return blocked('waiting_merge', 'merge_unverified')
        failure_code = 'inventory_readback_unavailable'
        try:
            inventory = self.observations.read_conditional_inventory(now,sdk.condition_id)
            quantities = inventory['quantities']
            if (inventory['condition_id'] != sdk.condition_id or tuple(inventory['token_ids']) != sdk.token_ids
                    or set(quantities) != set(sdk.token_ids) or inventory['as_of'] != now.isoformat()
                    or any(not isinstance(q, Decimal) or not q.is_finite() or q < 0 for q in quantities.values())):
                return blocked('waiting_readback', 'inventory_readback_invalid')
            recorded = {t:sum((p.quantity for p in state.positions if p.token_id == t),Decimal('0')) for t in sdk.token_ids}
            numbers = inventory_numbers(sdk.token_ids, quantities, recorded, inventory)
            if any(quantities.values()):
                safe_to_stop = False
            if recorded != quantities:
                return blocked('waiting_readback', 'inventory_mismatch')
            if any(quantities.values()):
                return blocked('waiting_inventory', 'inventory_remaining', detail=sdk.readback_blocker)
            uncertain = next((o for o in state.orders if not o.authoritative or o.status == OrderStatus.LIVE), None)
            if uncertain is not None and not execution:
                return blocked('waiting_orders', 'orders_unverified', order_id=uncertain.order_id, detail=sdk.readback_blocker)
            if not readback:
                return blocked('waiting_readback', 'reconciliation_unavailable', detail=sdk.readback_blocker)
            owned = set(sdk.owned_candidate_ids())
            failure_code = 'open_orders_unavailable'
            for page in sdk.client.list_open_orders():
                for order in page.items:
                    if not isinstance(order.id, str) or not order.id:
                        return blocked('waiting_readback', 'open_orders_invalid')
                    if order.id in owned:
                        return blocked('waiting_orders', 'orders_remaining', order_id=order.id)
        except Exception as error:
            from lp_v2.wait_policy import unexpected_error
            diagnostic = diagnostic or unexpected_error(error, 'balance' if failure_code == 'inventory_readback_unavailable' else 'open_orders')
            return blocked('waiting_readback', failure_code)
        if diagnostic is not None:
            self._clearance_blocker = dict(diagnostic, numbers=numbers, safe_to_stop=safe_to_stop)
        return None

    def _record_self_proof(self, runner, now, success):
        if self.control_run_id is None:
            return
        from lp_v2.rotation_state import runtime_proof_binding
        try:
            if runtime_proof_binding(self.config,self.action_id,self.window_end,self.control_run_id,on_error=self._startup_error) != self._proof_binding:
                raise ValueError('physical run changed')
        except Exception as error:
            self._startup_error(error)
            self._self_proofs.clear()
            return
        cid = runner.condition_id
        if not success:
            self._self_proofs.pop(cid, None)
            return
        moments = self._self_proofs.setdefault(cid, [])
        if not moments or now > moments[-1]:
            moments.append(now)
            del moments[:-2]

    def _runtime_binding_valid(self):
        from lp_v2.rotation_state import runtime_proof_binding
        try:
            return runtime_proof_binding(self.config,self.action_id,self.window_end,self.control_run_id,
                on_error=self._startup_error) == self._proof_binding
        except Exception as error:
            self._startup_error(error)
            return False

    def _startup_ready(self, runner, now):
        if self.control_run_id is None:
            return True
        from lp_v2.rotation_state import runtime_proof_binding, start_verification_passed
        try:
            if runtime_proof_binding(self.config,self.action_id,self.window_end,self.control_run_id,on_error=self._startup_error) != self._proof_binding:
                return False
        except Exception as error:
            self._startup_error(error)
            return False
        return (len(self._self_proofs.get(runner.condition_id, ())) == 2
                or start_verification_passed(self.config,self.action_id,self.window_end,self.control_run_id,now))

    def _startup_error(self, error):
        from lp_v2.wait_policy import unexpected_error
        if self._entry_blocker is None:
            self._entry_blocker = unexpected_error(error, 'startup_proof')

    def _transition(self, phase, reason, old, new, *, result='PASS', scan=None):
        scan = scan if scan is not None else self.scanner.latest()
        self.journal.append('rotation', dict(admission_proof=self._admission_proof if reason=='entry_verified' else None,
            old_market=old,new_market=new,reason=reason,
            score_version=SCORE_VERSION,score_at=scan.scan['finished_at'] if scan else None,
            scan_started_at=scan.scan['started_at'] if scan else None,
            scan_finished_at=scan.scan['finished_at'] if scan else None,
            action_id=self.action_id,window_end=self.window_end,phase=phase,result=result,at=self.clock()))
        self.phase, self.reason = phase, reason
        if phase == 'running' and reason == 'entry_verified':
            self.entered_count += 1

    def _historical_clearance_reason(self, now):
        for historical in self._runners.values():
            if historical is self.current or not (historical.sdk.journal.events('intent') or historical.sdk.journal.events('merge_intent')):
                continue
            waiting = self._clearance_reason(now,runner=historical)
            if waiting is not None:
                return 'historical_'+waiting
        return None

    def _enter(self, now, old, scan):
        if self.window_end is None or now >= self.window_end:
            self._entry_blocker = None
            self.reason = 'needs_new_on'
            return
        if self._next_retry_at is not None and now < self._next_retry_at:
            self.reason = 'no_revalidated_candidate'
            return
        self._entry_blocker = None
        failures = []
        # Historical uncertainty gates entry, never the active market's protection.
        waiting = self._historical_clearance_reason(now)
        if waiting is not None:
            self.reason = waiting
            return
        ranking = self._ranking(scan)
        if not scan or not scan.complete or not ranking.leader:
            self.reason = 'no_executable_candidate'
            return
        for candidate in ranking.candidates:
            if candidate.reason is not None:
                continue
            target = candidate.condition_id
            self._transition('revalidating', 'fresh_checks', old, target,scan=scan)
            source = 'candidate_market'
            try:
                plans = self.observations.read_fixed_market(now,target)
                plan = plans[0]['plan']
                fresh_ranking = rank_candidates((plan,),startup_cap=self.config.capital_usdc)
                if fresh_ranking.leader is None or plan.market.condition_id != target:
                    raise _CandidateCheckFailure('fresh candidate no longer executable')
                if any(not 0 <= (self.clock()-b.observed_at).total_seconds() <= 60 for b in plan.books):
                    raise _CandidateCheckFailure('stale entry books')
                source = 'public_check'
                checks = self.observations.public_preflight(now,target)
                from lp_v2.live_control.workflow import PUBLIC
                if not isinstance(checks,dict) or any(checks.get(key,{}).get('status') != 'PASS' for key in PUBLIC):
                    raise _CandidateCheckFailure('public checks')
                source = 'account_check'
                account = self.observations.read_account_readiness(now, capital_usdc=self.config.capital_usdc,
                    identity=self.account_identity,relayer=self.relayer,condition_id=target,
                    known_order_ids=tuple(oid for r in self._runners.values() for oid in r.sdk.owned_candidate_ids()))
                required = ('balance','allowance','closed_only','legacy_orders','region','relayer_auth','proxy_execution')
                if not isinstance(account,dict) or any(account.get(key,{}).get('status') != 'PASS' for key in required):
                    raise _CandidateCheckFailure('account checks')
                candidate_runner = self._runner_for(plan)
                if self._clearance_reason(self.clock(),runner=candidate_runner,require_accounting=True) is not None:
                    self._record_self_proof(candidate_runner, now, False)
                    self._entry_blocker = self._entry_blocker or self._clearance_blocker
                    raise _CandidateCheckFailure('target inventory or retained history is not cleared')
                if self.control_run_id is not None and not self._startup_ready(self.current, now):
                    self._transition('cleared', 'waiting_seed_start_verification', old, target, scan=scan)
                    return
                if self.clock() >= self.window_end:
                    self._transition('cleared', 'needs_new_on', old, None,scan=scan)
                    return
                activation = self.action_id+':'+str(len(self.journal.events()))
                candidate_runner.sdk.bind_entry(action_id=self.action_id,window_end=self.window_end,
                                                activation_id=activation)
            except Exception as error:
                from lp_v2.wait_policy import unexpected_error
                self._entry_blocker = self._entry_blocker or (dict(code='no_revalidated_candidate')
                    if isinstance(error, _CandidateCheckFailure) else unexpected_error(error, source))
                failures.append(dict(condition_id=target,source=source,diagnostic=(dict(code='no_revalidated_candidate',reason=str(error))
                    if isinstance(error,_CandidateCheckFailure) else unexpected_error(error,source))))
                continue
            # Once a target becomes active, an execution failure cannot cause
            # fallback into a second market. Its own recovery owns every intent.
            self._entry_blocker = None
            self._candidate_failures = []
            self._failed_sweeps = 0
            self._next_retry_at = None
            source_runner = candidate_runner if len(self._self_proofs.get(target,()))==2 else self.current
            moments = self._self_proofs.get(source_runner.condition_id,())
            self._admission_proof = None
            if self._proof_binding is not None and len(moments)==2 and self._runtime_binding_valid():
                from lp_v2.live_control.profiles import fingerprint
                self._admission_proof = dict(action_id=self.action_id,run_id=self.control_run_id,
                    window_end=self.window_end.isoformat(),process_sha256=fingerprint(self._proof_binding['process']),
                    source_market=source_runner.condition_id,moments=[at.isoformat() for at in moments])
            self.current = candidate_runner
            self._transition('running', 'entry_verified', old, target,scan=scan)
            self.current.step(self.clock())
            signed = self.current.sdk.journal.events('signed')
            if self.clock() >= self.window_end and not any(p.get('activation_id') == activation for _,p in signed):
                self._transition('cleared','needs_new_on',target,None)
            return
        self._candidate_failures = failures
        delay = (30,60,120,300)[min(self._failed_sweeps,3)]
        self._failed_sweeps += 1
        self._next_retry_at = now+timedelta(seconds=delay)
        self._entry_blocker = dict(self._entry_blocker or dict(code='no_revalidated_candidate'),candidates=failures)
        self.journal.append('revalidation',dict(status='FAIL',reason='revalidation_failed',
            candidates=failures,next_retry_at=self._next_retry_at,at=now))
        self._transition('cleared', 'no_revalidated_candidate', old, None,scan=scan)

    def _runner_for(self, plan):
        return self._runner_for_market(plan.market.condition_id, tuple(t.token_id for t in plan.market.tokens))

    def _runner_for_market(self, cid, tokens):
        from lp_v2.rotation_state import open_market_journal, validate_histories
        from lp_v2.live_sdk import LiveSDK
        from lp_v2.live_runtime import LiveRunner
        old = self.current.sdk
        validate_histories(self.config.state_dir,old.owner,candidate=cid)
        if cid in self._runners:
            runner = self._runners[cid]
            if runner.sdk.token_ids != tokens:
                raise ValueError('market tokens changed')
            runner.stop.clear()
            return runner
        identity = dict(owner=old.owner,condition_id=cid,token_ids=tokens)
        journal = open_market_journal(self.config.state_dir,identity)
        sdk = LiveSDK(old.client,journal,**identity,wallet=old.wallet,signer=old.signer,clock=self.clock)
        sdk.rpc,sdk.relayer,sdk.http = old.rpc,old.relayer,old.http
        runner = LiveRunner(self.config,sdk,self.observations,clock=self.clock)
        self._runners[cid] = runner
        return runner

    def drain(self, *, timeout_s=90, monotonic=None, wait=None):
        self.stop.set()
        if self.exit_code is not None:
            return self.exit_code
        # Historical runners include every old not-yet-cleared journal, never foreign orders.
        codes = [runner.drain(timeout_s=timeout_s,monotonic=monotonic,wait=wait)
                 for runner in self._runners.values()]
        self.exit_code = 0 if all(code == 0 for code in codes) else 1
        self.journal.append('rotation_stop',dict(at=self.clock(),exit_code=self.exit_code,
            markets=tuple(self._runners),action_id=self.action_id))
        with self._lock:
            self._snapshot = self._build(self.clock())
        return self.exit_code

    def close(self):
        if self.scanner.stop(30) and self.scanner.sdk is not self.observations:
            self.scanner.sdk.close()
        for runner in self._runners.values():
            if runner is not self.current:
                runner.sdk.journal.close()
        self.current.sdk.close()
        if hasattr(self.observations,'close'):
            self.observations.close()
        self.journal.close()

    def run(self):
        try:
            while not self.stop.is_set():
                self.scanner.start()
                self.step()
                self.stop.wait(1)
        finally:
            try:
                self.drain()
            finally:
                self.close()
