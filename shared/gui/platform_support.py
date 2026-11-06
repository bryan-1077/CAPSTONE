"""Host capabilities shared by the viewer, launcher, and diagnostics.

Keep OS-specific execution behind this boundary. Qt viewer availability is
verified separately by the smoke test; it does not imply worker support.
"""
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
import platform
import signal
import socket


@dataclass(frozen=True)
class HostCapabilities:
    system: str
    wsl: bool
    local_execution: bool
    execution_message: str

    def as_dict(self):
        return asdict(self)


def host_capabilities():
    system = platform.system()
    linux = system == 'Linux'
    wsl = linux and 'microsoft' in platform.release().lower()
    supported = (linux and Path('/proc/self/stat').is_file()
                 and Path('/proc/sys/kernel/random/boot_id').is_file()
                 and find_spec('fcntl') is not None
                 and hasattr(socket, 'AF_UNIX') and hasattr(signal, 'SIGUSR1'))
    if supported:
        message = 'Linux worker facilities are available. Design tools and server access require separate setup.'
    else:
        message = ('Starting and reconnecting to design runs currently requires Linux '
                   'with /proc and Unix worker facilities. You can still browse saved runs. '
                   'On Windows, launch inside WSL2 with a working GUI display.')
    return HostCapabilities(system, wsl, bool(supported), message)


def require_local_execution():
    host = host_capabilities()
    if not host.local_execution:
        raise ValueError(host.execution_message)
