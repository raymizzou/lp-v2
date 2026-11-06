"""Diagnostic deadlines only: this table grants no trading or release authority."""

# Seconds and an operator action, shared by round, rotation and handoff waits.
WAIT_POLICIES = {
    **dict.fromkeys(('unexpected_error', 'waiting_readback', 'inventory_readback_unavailable',
        'inventory_readback_invalid', 'open_orders_unavailable', 'open_orders_invalid',
        'order_readback_unavailable', 'trade_history_unavailable', 'trade_history_mismatch',
        'reconciliation_unavailable', 'round_readback_unavailable', 'current_unknown',
        'waiting_drain'), (600, 'Check exchange/network readback; provide summary to maintainer if persistent.')),
    **dict.fromkeys(('waiting_pending', 'pending_order', 'waiting_orders', 'orders_unverified',
        'orders_remaining'), (1800, 'Verify or cancel owned orders at exchange; retain unresolved reservations.')),
    **dict.fromkeys(('waiting_merge', 'merge_unverified'),
        (1800, 'Verify merge transaction/receipt with relayer; retain reservation.')),
    **dict.fromkeys(('inventory_mismatch', 'inventory_remaining', 'waiting_inventory',
        'CONDITIONAL_INVENTORY_UNMATCHED'),
        (3600, 'Check both token balances against owned inventory; ask operator to reconcile externally.')),
    **dict.fromkeys(('no_executable_candidate', 'no_revalidated_candidate', 'incomplete_scan'),
        (1800, 'Inspect scan coverage and candidate check failures; retry after correction.')),
    **dict.fromkeys(('waiting_seed_start_verification', 'STOPPED_HANDOFF_INCOMPLETE'),
        (0, 'Check bound startup/stopped-run proof with operator; existing admission gates remain.')),
    **dict.fromkeys(('needs_new_on', 'window_expired'),
        (0, 'Request a new explicitly confirmed live on authorization.')),
    **dict.fromkeys(('current_invalid', 'higher_score'),
        (3600, 'Inspect exit recovery blockers; wait for existing risk clearance.')),
}


def wait_policy(code):
    if code.startswith('historical_'):
        code = code.removeprefix('historical_')
    if code.startswith('unexpected_error:'):
        code = 'unexpected_error'
    return WAIT_POLICIES[code]


def wait_blocker(detail, previous, now):
    from datetime import timedelta
    seconds, next_step = wait_policy(detail['code'])
    same = previous is not None and previous['code'] == detail['code']
    since = previous['since'] if same else now
    deadline = since + timedelta(seconds=seconds)
    return dict(detail, since=since, deadline=deadline, escalated=now >= deadline,
                numbers=detail.get('numbers', 'UNKNOWN'), next_step=next_step,
                safe_to_stop=detail.get('safe_to_stop', 'UNKNOWN'))


def unexpected_error(error, source):
    import re
    # Type names can be supplied by external libraries; admit identifiers only.
    name = re.sub(r'[^A-Za-z0-9_]', '_', type(error).__name__)[:80]
    return dict(code=f'unexpected_error:{name}@{source}', order_tail=None)
