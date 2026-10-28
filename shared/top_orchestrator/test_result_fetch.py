"""Receipt retrieval retries never restart jobs or retry permission failures."""
import unittest
from unittest.mock import Mock, patch
from .validation import fetch_result_file


class ResultFetchTests(unittest.TestCase):
    def test_transient_missing_receipt_is_retried(self):
        ssh=Mock()
        ssh.fetch_file.side_effect=[{'ok':False,'error':'[Errno 2] No such file'}, {'ok':True}]
        with patch('shared.top_orchestrator.validation.time.sleep') as sleep:
            self.assertTrue(fetch_result_file(ssh,'remote/result.json','local.json')['ok'])
        self.assertEqual(ssh.fetch_file.call_count,2)
        sleep.assert_called_once_with(0.25)
        ssh.run.assert_not_called()

    def test_permission_failure_is_not_retried(self):
        ssh=Mock()
        ssh.fetch_file.return_value={'ok':False,'error':'Permission denied'}
        with patch('shared.top_orchestrator.validation.time.sleep') as sleep:
            self.assertFalse(fetch_result_file(ssh,'remote/result.json','local.json')['ok'])
        ssh.fetch_file.assert_called_once()
        sleep.assert_not_called()
        ssh.run.assert_not_called()

    def test_missing_receipt_has_a_fixed_bound(self):
        ssh=Mock()
        ssh.fetch_file.return_value={'ok':False,'error':'[Errno 2] No such file'}
        with patch('shared.top_orchestrator.validation.time.sleep') as sleep:
            self.assertFalse(fetch_result_file(ssh,'remote/result.json','local.json')['ok'])
        self.assertEqual(ssh.fetch_file.call_count,3)
        self.assertEqual(sleep.call_count,2)
        ssh.run.assert_not_called()
