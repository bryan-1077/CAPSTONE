"""Limits and conservative token reservations for specification recovery.

Reservations use the same input heuristic as TPM pacing plus the complete output
allowance. They bound requested work, not provider billing or actual token usage.
"""
from dataclasses import dataclass
import os

from specification import SpecificationError
from tpm_pacing import estimate_input


def positive_setting(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise SpecificationError(f'{name} must be a positive integer') from exc
    if value < 1:
        raise SpecificationError(f'{name} must be a positive integer')
    return value


@dataclass(frozen=True)
class RecoveryLimits:
    stage_attempts: int = 4
    semantic_cycles: int = 2
    max_calls: int = 12
    max_reserved_tokens: int = 400_000

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise SpecificationError(f'Recovery limit {name} must be a positive integer')

    @classmethod
    def from_environment(cls):
        return cls(
            positive_setting('SPECIFICATION_STAGE_ATTEMPTS', 4),
            positive_setting('SPECIFICATION_SEMANTIC_CYCLES', 2),
            positive_setting('SPECIFICATION_REPAIR_MAX_CALLS', 12),
            positive_setting('SPECIFICATION_REPAIR_MAX_TOKENS', 400_000))


class RepairBudgetExceeded(SpecificationError):
    pass


class RepairBudget:
    def __init__(self, limits):
        self.limits = limits
        self.calls = 0
        self.reserved_tokens = 0
        self.exhausted = False

    def wrap(self, caller, output_tokens):
        def bounded(prompt, model):
            needed = estimate_input([{'role': 'user', 'content': prompt}]) + output_tokens
            if self.calls >= self.limits.max_calls:
                self.exhausted = True
                raise RepairBudgetExceeded(f'Specification repair model-call limit reached ({self.limits.max_calls})')
            if self.reserved_tokens + needed > self.limits.max_reserved_tokens:
                self.exhausted = True
                raise RepairBudgetExceeded(
                    f'Specification repair token reservation limit reached: '
                    f'{self.reserved_tokens} reserved, next call needs {needed}, '
                    f'limit {self.limits.max_reserved_tokens}')
            self.calls += 1
            self.reserved_tokens += needed
            return caller(prompt, model)
        return bounded

    def snapshot(self):
        return {'model_calls': self.calls, 'max_model_calls': self.limits.max_calls,
                'reserved_tokens': self.reserved_tokens,
                'max_reserved_tokens': self.limits.max_reserved_tokens,
                'exhausted': self.exhausted,
                'token_accounting': 'estimated_input_plus_max_output'}
