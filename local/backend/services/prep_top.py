"""Static checks for the top-selection forms emitted by prep."""


def tcl_selects_top(script: str, top_module: str) -> bool:
    """Recognize literal tops and the generated file-backed TOP assignment.

    This is a prep contract check, not a Tcl interpreter. Unknown assignments
    invalidate the tracked value; comments and mere name mentions never prove
    that the intended top is elaborated.
    """
    import re

    if not top_module:
        return False
    paths = set()
    handles = set()
    top_matches = False
    elaborated = False

    def literal(value):
        value = value.strip()
        if len(value) >= 2 and (value[0], value[-1]) in (('"', '"'), ('{', '}')):
            value = value[1:-1]
        return value

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        assignment = re.fullmatch(r'set\s+(\w+)\s+(.+)', line)
        if assignment:
            name, value = assignment.groups()
            paths.discard(name)
            handles.discard(name)
            normalized = re.fullmatch(r'\[file normalize (.+)\]', value)
            path_value = normalized[1] if normalized else value
            if literal(path_value) in ('top_module.txt', './top_module.txt'):
                paths.add(name)
            opened = re.fullmatch(r'\[open (.+?) r\]', value)
            if opened and (literal(opened[1]) in ('top_module.txt', './top_module.txt')
                           or opened[1] in {'$' + item for item in paths}):
                handles.add(name)
            if name == 'TOP':
                read = re.fullmatch(r'\[string trim \[read \$(\w+)\]\]', value)
                if value == '[string trim $::env(TOP)]':
                    # The generator permits a runtime override, but must
                    # still provide a valid default from the prepared bundle.
                    continue
                top_matches = (literal(value) == top_module
                               or bool(read and read[1] in handles))
        command = re.fullmatch(r'elaborate\s+(.+)', line)
        if command:
            target = literal(command[1])
            if not (top_matches if target in ('$TOP', '${TOP}') else target == top_module):
                return False
            elaborated = True
    return elaborated
