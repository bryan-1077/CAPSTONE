"""Bounded interactive retries for rejected SSH credentials."""

import getpass
import warnings


def prompt_password(ssh):
    # Fail closed if the terminal cannot hide the credential.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        ssh.password = getpass.getpass(f"SSH password for {ssh.username}@{ssh.host}: ")


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
