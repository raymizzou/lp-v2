"""Validated current maker policy; never evidence about historical fill costs."""
from decimal import Decimal


class FeePolicyError(ValueError):
    pass


def maker_policy(trading):
    enabled, schedule = trading.fees_enabled, trading.fee_schedule
    if type(enabled) is not bool:
        raise FeePolicyError('FEE_FLAG_UNKNOWN')
    if schedule is None:
        if enabled is False:
            return False, Decimal('0'), Decimal('0')
        raise FeePolicyError('FEE_SCHEDULE_UNKNOWN')
    rate, rebate = schedule.rate, schedule.rebate_rate
    if (type(schedule.taker_only) is not bool or schedule.taker_only is not True
            or schedule.exponent != 1 or not rate.is_finite() or rate < 0
            or not rebate.is_finite() or not 0 <= rebate <= 1
            or (enabled is False and (rate != 0 or rebate != 0))):
        raise FeePolicyError('FEE_POLICY_UNSUPPORTED')
    return enabled, rate * 10000, rebate


def wire_maker_policy(wire):
    """Check wire booleans before the SDK's permissive boolean coercion."""
    from types import SimpleNamespace
    from polymarket.models.gamma.market import FeeSchedule
    enabled, raw = wire.get('feesEnabled'), wire.get('feeSchedule')
    if type(enabled) is not bool:
        raise FeePolicyError('FEE_FLAG_UNKNOWN')
    if raw is not None:
        if not isinstance(raw, dict) or type(raw.get('takerOnly')) is not bool:
            raise FeePolicyError('FEE_POLICY_UNSUPPORTED')
        if type(raw.get('exponent')) not in (int, float):
            raise FeePolicyError('FEE_POLICY_UNSUPPORTED')
        try:
            schedule = FeeSchedule.model_validate(raw)
        except Exception:
            raise FeePolicyError('FEE_SCHEDULE_UNKNOWN') from None
    else:
        schedule = None
    return maker_policy(SimpleNamespace(fees_enabled=enabled, fee_schedule=schedule))
