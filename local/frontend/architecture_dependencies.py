"""Combinational interface models for existing reusable datapaths.

Lists contain current-cycle input dependencies, not sequential state updates.
An empty list means no combinational path from data/control inputs to that output.
These models describe template contracts; generated RTL still needs lint/tests.
"""
REUSABLE_DEPENDENCIES = {
    'request_queue': {
        'enq_ready': ['deq_en', 'sel_idx'], 'req_array': [], 'req_valid': [],
    },
    'tRRD': {'tRRD_block': []},
    'tFAW_tracker': {'tFAW_ok': ['act_pulse'], 'tFAW_block': ['act_pulse'], 'act_count': []},
    'scheduler': {
        'sel_idx': ['req_array', 'req_valid', 'bank_active', 'bank_open_row', 'ref_req'],
        'issue_valid': ['req_array', 'req_valid', 'bank_active', 'bank_open_row', 'ref_req'],
        'issue_ref': ['ref_req', 'cmd_ready', 'timing_ok'],
        'issue_txn': ['req_array', 'req_valid', 'bank_active', 'bank_open_row', 'ref_req', 'cmd_ready', 'timing_ok'],
    },
    'scheduler_round_robin': {
        'issue_ref': ['ref_req', 'cmd_ready'],
        'issue_txn': ['ref_req', 'txn_valid', 'cmd_ready'],
    },
}


def dependency_cycle(connections, modules):
    """Return a directed cycle including declared intra-module paths, if any."""
    graph = {}
    def edge(source, target):
        graph.setdefault(source, []).append(target)
    for connection in connections:
        edge(connection['from'], connection['to'])
    for instance, module in modules.items():
        for output, inputs in module['dependencies'].items():
            for name in inputs:
                edge(f'{instance}.{name}', f'{instance}.{output}')
    active, visited, path = set(), set(), []
    def visit(node):
        if node in active:
            return path[path.index(node):] + [node]
        if node in visited:
            return None
        active.add(node)
        path.append(node)
        for target in graph.get(node, []):
            cycle = visit(target)
            if cycle:
                return cycle
        path.pop()
        active.remove(node)
        visited.add(node)
        return None
    for node in graph:
        cycle = visit(node)
        if cycle:
            return cycle
    return None
