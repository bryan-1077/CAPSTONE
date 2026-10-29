"""Experimental backend steps attach to the same allocation as verification."""

import json
import unittest
from unittest.mock import patch

import app
from services.build_names import full_flow_names
from workers.workers import _build_inner_ssh_command
from shared.top_orchestrator.allocation import ENVIRONMENT


class SharedBackendTests(unittest.TestCase):
    def test_every_backend_tool_uses_existing_six_cpu_allocation_without_nested_alias(self):
        state = dict(shared_allocation_id='777', shared_allocation_cpus=6)
        for command in ('genus -files synthesis.tcl', 'innovus -files implementation.tcl'):
            actual = _build_inner_ssh_command(state, command)
            self.assertIn('--jobid=777', actual)
            self.assertIn('--cpus-per-task=6', actual)
            self.assertIn('MAKEFLAGS=-j6', actual)
            self.assertNotIn('load-ecen-454', actual)
            self.assertNotIn('--partition=', actual)

    def test_legacy_path_remains_available_without_shared_mode(self):
        command = _build_inner_ssh_command({}, 'echo legacy')
        self.assertIn('--cpus-per-task=1', command)
        self.assertIn('load-ecen-454', command)

    def test_backend_inherits_and_validates_lease(self):
        state = dict(ssh_host='fake', ssh_user='user', ssh_port=22)
        lease = dict(job_id='777', cpus=6, host='fake', username='user', port=22)
        with patch.dict('os.environ', {ENVIRONMENT: json.dumps(lease)}):
            app._apply_shared_allocation(state)
            self.assertEqual(state['shared_allocation_id'], '777')
            with self.assertRaisesRegex(ValueError, 'different SSH'):
                app._apply_shared_allocation({**state, 'ssh_user': 'other'})

    def test_namespaced_outputs_preserve_script_depth_and_legacy_names(self):
        legacy = full_flow_names(5)
        test = full_flow_names(5, namespace='sa777')
        self.assertEqual(legacy['remote_mapped_dir'], 'build_mapped_200MHz_01')
        self.assertEqual(test['remote_mapped_dir'], 'build_mapped_sa777_200MHz_01')
        self.assertEqual(test['remote_mapped_script'].count('/'), 1)
        self.assertTrue(test['gdsii_script'].startswith('scripts_sa777_200MHz_01/'))


if __name__ == '__main__':
    unittest.main()
