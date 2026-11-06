"""Dedicated GUI worker; terminal interaction is disabled for this milestone."""
import json
import os
from pathlib import Path
import sys
import traceback
import signal
import time


def execute(config, password, directory, interaction=None):
    from shared.top_orchestrator import auth, orchestrator
    from shared.top_orchestrator.contracts import RunConfig
    from shared.top_orchestrator.remote import RemoteConfig
    from dataclasses import replace
    options = dict(config)
    description = options.pop('description', '')
    review = options.pop('review_frequency', False)
    options['input_yaml'] = Path(options['input_yaml']) if options.get('input_yaml') else None
    if options.get('remote_config'):
        options['remote_config'] = Path(options['remote_config'])
    options['ask_password'] = True
    options['shared_allocation'] = not (options.get('skip_verification') and options.get('no_backend'))
    run_config = RunConfig(entry='generate', **options)
    # Worker-local bridge until the shared interaction interface is implemented.
    # No input/getpass monkeypatch and no changes to the CLI process.
    def supply_credentials(selected):
        if selected.skip_verification and selected.no_backend:
            return
        nonlocal password
        if not password and interaction is not None:
            remote = RemoteConfig.load(selected.remote_config)
            host, username = remote.host, remote.username
            if selected.skip_verification:
                host = os.getenv('CAPSTONE_SSH_HOST') or 'olympus.ece.tamu.edu'
                username = os.getenv('CAPSTONE_SSH_USER') or 'antgamez1203'
            password = interaction.request('credential', dict(host=host, username=username))['value']
        if not password:
            raise ValueError('A password is required for remote execution in this GUI milestone.')
        remote = RemoteConfig.load(selected.remote_config)
        identities = {(remote.host, remote.username)}
        if selected.skip_verification:
            identities.add((os.getenv('CAPSTONE_SSH_HOST') or 'olympus.ece.tamu.edu',
                            os.getenv('CAPSTONE_SSH_USER') or 'antgamez1203'))
        for identity in identities:
            auth._passwords.get()[identity] = password
    def retry_credentials(ssh):
        if interaction is None:
            raise ValueError('Interactive password retry unavailable')
        response = interaction.request('credential', dict(host=ssh.host, username=ssh.username,
            reason='SSH authentication was rejected. Enter another password.'))
        ssh.password = str(response.get('value', ''))
        if not ssh.password:
            raise ValueError('Password must not be empty')
        if auth._passwords.get() is not None:
            auth._passwords.get()[(ssh.host, ssh.username)] = ssh.password

    original_generation = orchestrator.prepare_generation
    def prepare_generation(selected, run_dir):
        if description:
            from .specification import select_specification
            selected = replace(selected, input_yaml=select_specification(description, run_dir, interaction))
        if review:
            from .specification import review_frequency
            selected = review_frequency(selected, interaction, run_dir)
        return original_generation(selected, run_dir)

    from shared.top_orchestrator import allocation
    original_close = allocation.AllocationSession.close
    def close_allocation(session):
        # Retain the account and credential only long enough to verify release.
        ssh = session.ssh
        credential = ssh.password if ssh is not None else None
        original_close(session)
        if ssh is None:
            return
        from shared.remote.ssh_executor import SSHExecutor
        import shlex
        remote = RemoteConfig.load(session.config.remote_config)
        checker = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
        checker.password = credential
        selector = ['--name', session.job_name]
        try:
            result = checker.run(shlex.join(['squeue', '--noheader', *selector, '--format=%i']), timeout=15)
            session.record['queue_confirmation'] = result
            session.record['cleanup_status'] = ('terminated' if result.get('ok') and not result.get('stdout', '').strip()
                                                  else 'cleanup_unconfirmed')
        except Exception as exc:
            session.record['cleanup_status'] = 'cleanup_unconfirmed'
            session.record['queue_confirmation_error'] = type(exc).__name__
        finally:
            checker.close()
            checker.password = None
            session.save()

    original_nodes = dict(orchestrator.NODES)
    def guarded_node(name, function):
        def invoke(selected, run_dir):
            if interaction is not None and interaction.cancelled:
                from .lifecycle import RunCancelled
                raise RunCancelled()
            if name != 'backend':
                return function(selected, run_dir)
            remote = RemoteConfig.load(selected.remote_config)
            updates = allocation.backend_environment(remote)
            updates.update(CAPSTONE_SSH_HOST=remote.host, CAPSTONE_SSH_USER=remote.username,
                           CAPSTONE_SSH_PORT=str(remote.port), CAPSTONE_SSH_KEY=remote.key_filename or '')
            old = {key: os.environ.get(key) for key in updates}
            os.environ.update(updates)
            try:
                return function(selected, run_dir)
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        return invoke

    original_retry = auth.prompt_password
    original = orchestrator.prepare_password
    orchestrator.prepare_password = supply_credentials
    allocation.AllocationSession.close = close_allocation
    orchestrator.NODES.update({name: guarded_node(name, fn) for name, fn in original_nodes.items()})
    auth.prompt_password = retry_credentials
    orchestrator.prepare_generation = prepare_generation
    try:
        return orchestrator.run(run_config, directory)
    finally:
        orchestrator.prepare_password = original
        orchestrator.prepare_generation = original_generation
        auth.prompt_password = original_retry
        allocation.AllocationSession.close = original_close
        orchestrator.NODES.update(original_nodes)


def main():
    from .registry import ACTIVE, read_record, atomic_write
    from .run_storage import RunStorage, RunOutput
    from .lifecycle import RunCancelled, cleanup_local, remote_cleanup
    from .interaction import Interaction
    directory = Path(sys.argv[1])
    lock_fd, channel_fd = int(sys.argv[2]), int(sys.argv[3])
    os.set_inheritable(lock_fd, False)
    with os.fdopen(channel_fd) as channel:
        request = json.load(channel)
    directory.parent.mkdir(parents=True, exist_ok=True)
    record = read_record(ACTIVE)
    record.update(lifecycle='running')
    storage = RunStorage(directory, record)
    interaction = None
    cancelling = False
    def interrupted(number, frame):
        nonlocal cancelling
        if not cancelling:
            cancelling = True
            raise RunCancelled()
    signal.signal(signal.SIGUSR1, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    outcome, code = 'failed', 1
    startup_message = 'Worker stopped before the design flow initialized.'
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with storage.log.open('w', encoding='utf-8', buffering=1) as stream:
        output = RunOutput(storage, stream)
        sys.stdout = output
        sys.stderr = output
        try:
            interaction = Interaction(sys.argv[4], worker_id=record['worker_id'],
                                      cancel_callback=lambda: os.kill(os.getpid(), signal.SIGUSR1))
            state = execute(request['config'], request.pop('password', ''), directory, interaction)
            outcome = state.status
            code = 0 if outcome == 'complete' else 2
        except RunCancelled:
            outcome, code = 'cancelled', 130
            startup_message = 'Run cancelled before initialization completed.'
            print('Run cancelled; adapter cleanup attempted.', flush=True)
        except BaseException as exc:
            startup_message = f'{type(exc).__name__}: {exc}'
            if cancelling:
                outcome, code = 'cancelled', 130
            traceback.print_exc()
        finally:
            # A second stop must not interrupt cleanup or launch another signal.
            signal.signal(signal.SIGUSR1, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            local = cleanup_local()
            remote = remote_cleanup(directory, not (request['config'].get('skip_verification')
                                                    and request['config'].get('no_backend')))
            record.update(lifecycle=outcome, returncode=code, finished=time.time(),
                          local_cleanup=local, remote_cleanup=remote)
            storage.publish(create=True)
            if not (directory / 'state.json').exists():
                atomic_write(directory / 'state.json', dict(
                    config=request['config'], status=outcome, current_stage=None,
                    results=[dict(stage='generate', status=outcome, message=startup_message, artifacts={})]))
            atomic_write(directory / 'gui_lifecycle.json', record)
            # The lock is still held: another worker cannot replace this entry yet.
            atomic_write(ACTIVE, record)
            if interaction is not None:
                interaction.close()
            try:
                Path(sys.argv[4]).parent.rmdir()
            except OSError:
                pass
    sys.stdout, sys.stderr = original_stdout, original_stderr
    return code


if __name__ == '__main__':
    raise SystemExit(main())
