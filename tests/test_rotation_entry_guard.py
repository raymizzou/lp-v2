"""Missing on metadata must retain the public-market opening guard."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from lp_v2.rotation_live import RotationRunner
from lp_v2.rotation_scan import ScanWorker
from tests.test_live_runtime import inputs, world
from tests.test_live_sdk import AT, CID


@pytest.mark.parametrize('valid', [False, None])
def test_missing_action_requires_affirmative_market(tmp_path, valid):
    plan = inputs(yes='.40', no='.40')
    current, exchange, sdk, journal, observations = world(tmp_path, (plan,))
    config = replace(current.config, state_dir=str(tmp_path), market=CID)
    rotation = RotationRunner(config, current, ScanWorker(observations),
                              action_id=None, window_end=None, clock=lambda: exchange.now)
    try:
        rotation.step(AT)
        assert [order.token_id for order in exchange.posts] == ['101', '102']
        requests = current.ledger.snapshot().requests

        observations.plans = (() if valid is None else
                              (replace(plan, market=replace(plan.market, accepting_orders=False)),))
        exchange.now = AT + timedelta(seconds=10)
        report = rotation.step(exchange.now)

        # Within the quote hold period, only the denied opening permission retires these orders.
        assert [call.kwargs['order_id'] for call in exchange.sdk.cancel_order.call_args_list] == ['order-1', 'order-2']
        assert all(order.status == 'CANCELED' for order in exchange.orders.values())
        assert len(exchange.posts) == 2
        assert current.ledger.snapshot().requests == requests
        assert current.ledger.snapshot().capital.total == Decimal('10')
        assert current.ledger.snapshot().startup_cap == config.capital_usdc
        assert journal.events('merge_intent') == ()
        assert report['rotation']['phase'] == 'running'
        assert report['rotation']['reason'] == 'entry_authorization_missing'
    finally:
        rotation.journal.close()
        sdk.close()
