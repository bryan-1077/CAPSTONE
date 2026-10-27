"""Per-module recovery accounting. No API calls or approval decisions."""
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

from llm_diagnostics import redact


active_recovery = ContextVar('rtl_recovery', default=None)


class RecoveryBudget:
    MAX_EXTRA = 2
    MAX_WAIT = 30.0

    def __init__(self):
        self.used = {'payload': 0, 'transport': 0, 'truncation': 0}
        self.events = []
        self.wait_seconds = 0.0

    def deny(self, category, stage, cause, reason):
        self.events.append(redact({'category': category, 'stage': stage, 'decision': 'denied',
                                   'reason': reason, 'cause': cause}))
        return False

    def claim(self, category, stage, cause, *, delay=0.0):
        if category not in self.used:
            raise ValueError('Unknown recovery category')
        if not math.isfinite(delay) or delay < 0 or self.wait_seconds + delay > self.MAX_WAIT:
            return self.deny(category, stage, cause, 'retry_delay_limit')
        if self.used[category]:
            return self.deny(category, stage, cause, 'category_limit')
        if sum(self.used.values()) >= self.MAX_EXTRA:
            return self.deny(category, stage, cause, 'module_limit')
        self.used[category] += 1
        self.wait_seconds += delay
        self.events.append(redact({'category': category, 'stage': stage, 'decision': 'reserved',
                                   'delay_seconds': delay, 'cause': cause}))
        return True

    def report(self):
        return {'enabled': True, 'limits': {'extra_attempts': self.MAX_EXTRA, 'per_category': 1,
                'retry_delay_seconds': self.MAX_WAIT}, 'used': dict(self.used),
                'extra_attempts_reserved': sum(self.used.values()), 'wait_seconds': self.wait_seconds,
                'events': list(self.events)}


def retry_delay(value=None, *, now=None):
    """Honor seconds or HTTP dates. Larger delays are denied, never shortened."""
    if value is None:
        return 2.0
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else float('inf')
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, target.timestamp() - (now if now is not None else datetime.now(timezone.utc).timestamp()))
        except (ValueError, TypeError, OverflowError):
            return 2.0


def transport_retry(stage, cause, *, retry_after=None):
    budget = active_recovery.get()
    if budget is None:
        return False
    delay = retry_delay(retry_after)
    if not budget.claim('transport', stage, cause, delay=delay):
        return False
    print(f'[dual_llm] Recovery: transient retry in {delay:g}s (shared module budget).', flush=True)
    time.sleep(delay)
    return True
