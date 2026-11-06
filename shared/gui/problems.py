"""User-facing interpretation of saved failures; execution outcomes stay intact."""

CONNECTION_TITLE = 'Server connection error'
CONNECTION_REASON = 'Could not connect to the design server.'
CONNECTION_GUIDANCE = ('Check that your VPN is connected and the server is reachable, '
                       'then start a new run. VPN status cannot be determined from this error alone.')


def is_connection_error(result):
    if result.get('status') != 'failed' or result.get('stage') not in (
            'validation', 'backend', 'gdsii-validation', 'remote-check'):
        return False
    message = str(result.get('message', '')).lower()
    # Do not label model, simulation, queue-wait, or remote-command timeouts as
    # connection failures. Match explicit transport errors and the allocation
    # wrapper's bare socket timeout (command deadlines use different wording).
    return any(marker in message for marker in (
        'novalidconnectionserror', 'connection refused', 'no route to host',
        'network is unreachable', 'name or service not known',
        'temporary failure in name resolution', 'connection timed out',
        'shared slurm allocation failed: timeouterror: timed out',
    ))


def run_connection_error(state):
    failed = next((result for result in reversed(state.get('results', []))
                   if result.get('status') == 'failed'), {})
    return state.get('status') == 'failed' and is_connection_error(failed)
