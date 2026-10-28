"""Automatic project-shell cleanup requires idle evidence and exact ownership."""

import io
import json
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shared.top_orchestrator.legacy_allocations import cleanup_idle_legacy, eligible, PROBE
from shared.top_orchestrator.remote import RemoteConfig


DETAILS = '''JobId=720746 JobName=ecen-454
 UserId=tester(123) JobState=RUNNING
 RunTime=03:35:28 NumCPUs=1
 Command=bash
 WorkDir=/project
'''


class InspectionSSH:
    def __init__(self, *, idle=True, details=DETAILS, changed=False):
        self.idle = idle
        self.details = details
        self.changed = changed
        self.commands = []
        self.inspections = 0

    def run(self, command, **kwargs):
        self.commands.append(command)
        args = shlex.split(command)
        if args[0] == 'squeue':
            output = '720746|ecen-454|RUNNING\n'
        elif args[0] == 'scontrol':
            self.inspections += 1
            output = self.details
            if self.changed and self.inspections > 1:
                output = output.replace('WorkDir=/project', 'WorkDir=/other')
        elif args[0] == 'srun':
            output = json.dumps({'idle': self.idle, 'identity': ['100', '42']})
        else:
            output = ''
        return dict(ok=True, stdout=output, stderr='', exit_code=0)


class LegacyAllocationTests(unittest.TestCase):
    remote = RemoteConfig(host='fake', username='tester', project_dir='/project', partition='test', qos='test')

    def test_verified_idle_project_shell_is_automatically_released(self):
        ssh = InspectionSSH()
        result = cleanup_idle_legacy(ssh, self.remote, lambda *args: None)
        self.assertEqual(result['released'][0]['job_id'], '720746')
        self.assertEqual(ssh.commands[-1], 'scancel 720746')
        self.assertIn('--overlap', next(command for command in ssh.commands if command.startswith('srun ')))
        self.assertEqual(ssh.inspections, 2)

    def test_active_work_and_unknown_process_state_are_retained(self):
        for idle in (False, None):
            with self.subTest(idle=idle):
                ssh = InspectionSSH(idle=idle)
                result = cleanup_idle_legacy(ssh, self.remote, lambda *args: None)
                self.assertFalse(result['released'])
                self.assertFalse(any(command.startswith('scancel') for command in ssh.commands))

    def test_other_users_projects_commands_cpu_counts_and_recent_shells_are_excluded(self):
        replacements = [('tester(123)', 'other(456)'), ('WorkDir=/project', 'WorkDir=/different'),
                        ('Command=bash', 'Command=genus'), ('NumCPUs=1', 'NumCPUs=6'),
                        ('JobName=ecen-454', 'JobName=unrelated'), ('03:35:28', '00:05:00')]
        for old, new in replacements:
            with self.subTest(change=new):
                details = DETAILS.replace(old, new)
                self.assertFalse(eligible(details, self.remote))
                ssh = InspectionSSH(details=details)
                result = cleanup_idle_legacy(ssh, self.remote, lambda *args: None)
                self.assertFalse(result['released'])
                self.assertFalse(any(command.startswith(('scancel', 'srun')) for command in ssh.commands))

    def test_changed_job_identity_prevents_cancellation(self):
        ssh = InspectionSSH(changed=True)
        result = cleanup_idle_legacy(ssh, self.remote, lambda *args: None)
        self.assertFalse(result['released'])
        self.assertFalse(any(command.startswith('scancel') for command in ssh.commands))

    def test_process_probe_requires_one_sleeping_childless_shell_and_no_other_workload(self):
        def probe(*, children='', other_step=None, comm='bash', state='S'):
            class ProcPath:
                def __init__(self, parts): self.parts = parts
                def __truediv__(self, value): return ProcPath(self.parts + [value])
                def joinpath(self, *parts): return ProcPath(self.parts + list(parts))
                def read_text(self):
                    if self.parts[-1] == 'comm': return comm
                    if self.parts[-1] == 'children': return children
                    return '100 (bash) ' + state + ' ' + ' '.join(['0'] * 19)
            rows = 'PID JOBID STEPID LOCALID GLOBALID\n100 720746 0 0 0\n999 720746 9 0 0\n'
            if other_step:
                rows += '200 720746 ' + other_step + ' 0 0\n'
            with patch('sys.argv', ['probe', '720746']), patch.dict('os.environ', {'SLURM_STEP_ID': '9'}), \
                    patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=rows)), \
                    patch('pathlib.Path', side_effect=lambda path: ProcPath([path])), patch('time.sleep'), \
                    patch('sys.stdout', io.StringIO()) as output:
                exec(PROBE, {})
            return json.loads(output.getvalue())['idle']
        self.assertTrue(probe())
        for arguments in (dict(children='101'), dict(other_step='2'), dict(other_step='extern'),
                          dict(comm='genus'), dict(state='R')):
            with self.subTest(arguments=arguments):
                self.assertFalse(probe(**arguments))


if __name__ == '__main__':
    unittest.main()
