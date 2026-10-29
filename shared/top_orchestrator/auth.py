"""Bounded interactive retries for rejected SSH credentials."""

import getpass
import os
import warnings
from contextlib import contextmanager
from contextvars import ContextVar


_passwords = ContextVar("orchestrator_ssh_passwords", default=None)


@contextmanager
def password_session():
    passwords = {}
    token = _passwords.set(passwords)
    try:
        yield
    finally:
        passwords.clear()
        _passwords.reset(token)


def cached_password(host, username):
    return (_passwords.get() or {}).get((host, username))


def prepare_password(config):
    """Collect remote credentials after selections, before starting the flow."""
    if config.entry == "remote-check" or (
            config.entry in ("generate", "debug") and config.skip_verification and config.no_backend):
        return
    if (config.entry == "backend" and config.validation_result is None) or config.skip_verification:
        host = os.getenv("CAPSTONE_SSH_HOST") or "olympus.ece.tamu.edu"
        username = os.getenv("CAPSTONE_SSH_USER") or "antgamez1203"
    else:
        from .remote import RemoteConfig
        remote = RemoteConfig.load(config.remote_config)
        host, username = remote.host, remote.username
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass(
            f"Enter SSH password for '{username}' for Verification and Physical Design portions of the flow: ")
    if not password:
        raise ValueError("SSH password is required for the remote portions of the flow")
    _passwords.get()[(host, username)] = password


def prompt_password(ssh):
    # Fail closed if the terminal cannot hide the credential.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        ssh.password = getpass.getpass(f"SSH password for {ssh.username}@{ssh.host}: ")
    if _passwords.get() is not None:
        _passwords.get()[(ssh.host, ssh.username)] = ssh.password


def run_with_password_retries(ssh, command, *, ask_password, **kwargs):
    """Run the initial login command with at most three password re-prompts."""
    result = ssh.run(command, **kwargs)
    for retry in range(1, 4):
        if not ask_password or result.get("error_type") != "AuthenticationException":
            break
        ssh.close()
        ssh.password = None
        output = kwargs.get("on_output")
        if output:
            output("stderr", f"SSH authentication rejected. Check username/password; password retry {retry}/3.\n")
        prompt_password(ssh)
        result = ssh.run(command, **kwargs)
    return result
