"""Offline recovery limits, reservations and the recorded acceptance-binding failure."""
import copy
import os
import unittest
from unittest.mock import Mock, patch

from architecture import fingerprint
from specification import SpecificationError
from specification_edits import apply_architecture_edits
from specification_recovery import RecoveryLimits, RepairBudget, RepairBudgetExceeded


class SpecificationRecoveryTests(unittest.TestCase):
    def test_defaults_and_environment_overrides_validate_before_calls(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RecoveryLimits.from_environment(), RecoveryLimits(4, 2, 12, 400_000))
        settings = {'SPECIFICATION_STAGE_ATTEMPTS': '5', 'SPECIFICATION_SEMANTIC_CYCLES': '3',
                    'SPECIFICATION_REPAIR_MAX_CALLS': '8', 'SPECIFICATION_REPAIR_MAX_TOKENS': '200000'}
        with patch.dict(os.environ, settings):
            self.assertEqual(RecoveryLimits.from_environment(), RecoveryLimits(5, 3, 8, 200000))
            for key in settings:
                for bad in ('0', '-1', 'x'):
                    with self.subTest(key=key, bad=bad), patch.dict(os.environ, {key: bad}):
                        with self.assertRaisesRegex(SpecificationError, key):
                            RecoveryLimits.from_environment()
        with self.assertRaises(SpecificationError):
            RecoveryLimits(stage_attempts=True)

    def test_call_budget_is_shared_and_does_not_refund_failed_calls(self):
        budget = RepairBudget(RecoveryLimits(max_calls=2))
        generate = Mock(side_effect=SpecificationError('provider failed'))
        review = Mock(return_value='review')
        with self.assertRaisesRegex(SpecificationError, 'provider failed'):
            budget.wrap(generate, 24000)('prompt', 'fixture')
        self.assertEqual(budget.wrap(review, 6000)('prompt', 'fixture'), 'review')
        with self.assertRaises(RepairBudgetExceeded):
            budget.wrap(review, 4000)('reference correction', 'fixture')
        self.assertEqual(review.call_count, 1)
        self.assertEqual(budget.snapshot()['model_calls'], 2)

    def test_token_budget_refuses_call_before_invoking_provider(self):
        budget = RepairBudget(RecoveryLimits(max_reserved_tokens=1))
        caller = Mock()
        with self.assertRaisesRegex(RepairBudgetExceeded, 'token reservation limit'):
            budget.wrap(caller, 6000)('prompt', 'fixture')
        caller.assert_not_called()
        self.assertEqual(budget.snapshot()['reserved_tokens'], 0)

    def test_recorded_request_accept_repairs_require_endpoint_leaf(self):
        # Binding and three rejected operations from the November 12 run. The
        # complete architecture was not supplied; this isolates atomic admission.
        base = {'verification': {'events': {'request_accept': {
            'payload': {'request': {'endpoint': 'request_queue.dispatch_request'}}}}}}
        request_path = '/verification/events/request_accept/payload/request'
        edits = [
            {'op': 'remove', 'path': request_path},
            {'op': 'set', 'path': '/verification/events/request_accept/payload', 'value': {
                'write': {'endpoint': 'top.req_write'}, 'address': {'endpoint': 'top.req_addr'},
                'wdata': {'endpoint': 'top.req_wdata'}}},
            {'op': 'set', 'path': request_path, 'value': {'endpoint': 'request_queue.accepted_request'}}]
        before = copy.deepcopy(base)
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaisesRegex(SpecificationError, 'Edit individual fields'):
                apply_architecture_edits(base, {'base_fingerprint': fingerprint(base), 'edits': [edit]}, narrow=True)
            self.assertEqual(base, before)
        corrected = {'op': 'set', 'path': request_path + '/endpoint', 'value': 'request_queue.accepted_request'}
        result = apply_architecture_edits(base, {'base_fingerprint': fingerprint(base), 'edits': [corrected]}, narrow=True)
        self.assertEqual(result['verification']['events']['request_accept']['payload']['request']['endpoint'],
                         'request_queue.accepted_request')
        self.assertEqual(base, before)


if __name__ == '__main__':
    unittest.main()
