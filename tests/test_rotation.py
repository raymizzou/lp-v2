from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from tests.test_scenarios import AT, plan


def test_best_executable_candidate():
    from lp_v2.rotation import rank_candidates
    from lp_v2.scoring import SCORE_VERSION

    too_large = plan('too-large', quantity='101', reward=1000)
    no_pair = plan('no-pair', yes='0.70', no='0.70', reward=900)
    executable = plan('second', yes='0.40', no='0.40', reward=100)
    ranked = rank_candidates((too_large, no_pair, executable), startup_cap=D('100'))
    assert ranked.score_version == SCORE_VERSION
    assert ranked.leader.condition_id == 'second'
    assert {row.condition_id: row.reason for row in ranked.candidates} == {
        'too-large': 'capital_limit', 'no-pair': 'no_quote_pair', 'second': None}


def test_no_eligible_candidate():
    from lp_v2.rotation import evaluate_rotation, rank_candidates

    invalid = replace(plan('invalid'), market=replace(plan('invalid').market, accepting_orders=False))
    ranking = rank_candidates((invalid,), startup_cap=D('100'))
    result = evaluate_rotation(ranking, current=None, phase='cleared', complete=True,
                               current_valid=False, now=AT, window_end=AT+timedelta(minutes=5))
    assert ranking.leader is None
    assert result.action == 'hold'
    assert result.target is None
    assert result.reason == 'no_executable_candidate'


def test_rotation_transition_rules():
    from lp_v2.rotation import evaluate_rotation, rank_candidates

    a, b = plan('a', reward=100), plan('b', reward=200)
    for other, complete, action, reason in (
        (b, True, 'exit', 'higher_score'),
        (replace(b, market=replace(b.market, rewards_daily_rate=100)), True, 'hold', 'current_leads_or_ties'),
        (b, False, 'hold', 'incomplete_scan'),
    ):
        ranking = rank_candidates((a, other), startup_cap=D('100'))
        result = evaluate_rotation(ranking, current='a', phase='running', complete=complete,
            current_valid=True, now=AT, window_end=AT+timedelta(minutes=5))
        assert (result.action, result.reason) == (action, reason)
        assert result.target == ('b' if action == 'exit' else None)


def test_recover_current_leader_and_expired_window():
    from lp_v2.rotation import evaluate_rotation, rank_candidates

    a, b = plan('a', reward=200), plan('b', reward=100)
    ranking = rank_candidates((a, b), startup_cap=D('100'))
    result = evaluate_rotation(ranking, current='a', phase='exiting', complete=True,
        current_valid=True, now=AT, window_end=AT+timedelta(minutes=5))
    assert (result.action, result.target, result.reason) == ('resume', 'a', 'current_leads_again')
    ranking = rank_candidates((a, replace(b, market=replace(b.market, rewards_daily_rate=1000))), startup_cap=D('100'))
    for phase, valid, current, expected in (
        ('running', True, 'a', ('hold', None, 'window_expired')),
        ('running', False, 'a', ('exit', None, 'current_invalid')),
        ('cleared', False, None, ('hold', None, 'needs_new_on')),
    ):
        result = evaluate_rotation(ranking, current=current, phase=phase, complete=True,
            current_valid=valid, now=AT+timedelta(minutes=5), window_end=AT+timedelta(minutes=5))
        assert (result.action, result.target, result.reason) == expected
    result = evaluate_rotation(ranking, current=None, phase='cleared', complete=True,
        current_valid=False, now=AT, window_end=AT+timedelta(minutes=5))
    assert (result.action, result.target) == ('enter', 'b')
