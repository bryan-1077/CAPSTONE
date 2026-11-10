"""Retrieve optional screenshots from the final recorded implementation build."""
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, NamedTemporaryFile
from shared.remote.ssh_executor import SSHExecutor


def fetch_atomic(ssh, remote, destination):
    """Never expose an incomplete transfer to the GUI's polling reader."""
    destination = Path(destination)
    with TemporaryDirectory(prefix='.download-', dir=destination.parent) as tmp:
        staged = Path(tmp) / destination.name
        result = ssh.fetch_file(str(remote), staged, timeout=20)
        if result.get('ok') and staged.is_file():
            staged.replace(destination)
            return True
    return False


def collect_layout_images(state, ssh_config, destination, *, stages=None):
    requested = tuple(dict.fromkeys(stages)) if stages is not None else ('floorplan', 'placement', 'routing')
    if not requested or any(stage not in ('floorplan', 'placement', 'routing') for stage in requested):
        raise ValueError('Select floorplan, placement, or routing images.')
    build = state.get('outputs', {}).get('gdsii_remote_dir')
    if not build:
        return {'status': 'unavailable', 'reason': 'No implementation build recorded.'}
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    receipt = {'remote_build': build, 'images': [], 'status': 'unavailable', 'downloaded_stages': []}
    previous = {}
    try:
        previous = json.loads((destination / 'collection.json').read_text())
        if not isinstance(previous, dict) or previous.get('remote_build') != build:
            previous = {}
    except (OSError, ValueError):
        pass
    ssh = SSHExecutor(**ssh_config)
    try:
        ssh.connect(timeout=10)
        remote = PurePosixPath(build) / 'results/screenshots'
        manifest_path = destination / 'manifest.json'
        if not fetch_atomic(ssh, remote / 'manifest.json', manifest_path):
            receipt['reason'] = 'Screenshot manifest could not be retrieved.'
        elif manifest_path.stat().st_size > 512 * 1024:
            receipt['reason'] = 'Screenshot manifest exceeds the reader limit.'
        else:
            manifest = json.loads(manifest_path.read_text())
            entries = manifest.get('images', []) if isinstance(manifest, dict) else []
            for stage in requested:
                entry = next((item for item in entries if isinstance(item, dict)
                              and item.get('stage') == stage), {})
                record = {'stage': stage, 'status': 'unavailable',
                          'source_build': entry.get('source_build', build),
                          'origin': entry.get('origin', 'current_build')}
                if entry.get('status') == 'success':
                    # Fixed filenames prevent remote manifest paths from escaping
                    # either the selected build or the local artifact folder.
                    target = destination / (stage + '.png')
                    if fetch_atomic(ssh, remote / target.name, target):
                        record.update(status='available', file=target.name)
                        receipt['downloaded_stages'].append(stage)
                    else:
                        record['reason'] = 'Image could not be retrieved.'
                else:
                    record['reason'] = str(entry.get('error') or 'Capture was not recorded as successful.')
                receipt['images'].append(record)
            available = sum(item['status'] == 'available' for item in receipt['images'])
            receipt['status'] = 'available' if available == 3 else 'partial' if available else 'unavailable'
    except Exception as exc:
        receipt['reason'] = ('SSH authentication failed. Check your password or SSH key.'
                             if type(exc).__name__ in ('AuthenticationException', 'BadAuthenticationType')
                             else f'Screenshot retrieval failed ({type(exc).__name__}).')
    finally:
        ssh.close()
    # Retain earlier successful downloads from this same build if a retry fails.
    for old in previous.get('images', []):
        stage = old.get('stage') if isinstance(old, dict) else None
        if stage not in ('floorplan', 'placement', 'routing') or old.get('status') != 'available':
            continue
        if not (destination / (stage + '.png')).is_file():
            continue
        current = next((entry for entry in receipt['images'] if entry.get('stage') == stage), None)
        if current is None:
            receipt['images'].append(old)
        elif current.get('status') != 'available':
            current.update(old)
    available = sum(entry.get('status') == 'available' for entry in receipt['images'])
    receipt['status'] = 'available' if available == 3 else 'partial' if available else 'unavailable'
    with NamedTemporaryFile(mode='w', dir=destination, prefix='.collection-', delete=False) as handle:
        json.dump(receipt, handle, indent=2)
        temporary = Path(handle.name)
    temporary.replace(destination / 'collection.json')
    return receipt
