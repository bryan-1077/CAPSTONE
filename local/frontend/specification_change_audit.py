"""Read-only JSON change audit; no semantic inference or repair application."""
import copy
import json


def audit_changes(before, after):
    """Return deterministic object-field changes, treating lists as whole values.

    Inputs are JSON-compatible specification projections. Paths use RFC 6901.
    Entries describe evidence, not approved edits or behavioral correctness.
    """
    changes = []

    def visit(left, right, path):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                target = path + '/' + key.replace('~', '~0').replace('/', '~1')
                if key not in right:
                    changes.append({'op': 'remove', 'path': target, 'before': copy.deepcopy(left[key])})
                elif key not in left:
                    changes.append({'op': 'add', 'path': target, 'after': copy.deepcopy(right[key])})
                else:
                    visit(left[key], right[key], target)
        elif type(left) is not type(right) or json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
            changes.append({'op': 'replace', 'path': path,
                            'before': copy.deepcopy(left), 'after': copy.deepcopy(right)})

    visit(before, after, '')
    return changes
