# Services Overview

This directory holds non-agent helper services that support the EDA flow.

- `stage_routing.py`: Normalizes reviewer routing fields like owner, class, and reroute target so orchestration can decide whether a failure stays in the current stage or routes back to an earlier one.
- `path_naming.py`: Resolves and derives the remote prep, netlist, mapped, and GDSII build directory names used across stages.
- `synthesis_recipe_library.py`: Defines the deterministic synthesis-adaptation recipe library and default mapping hints used during prep review and prep edit planning, including top-specific mappings plus reusable interface-type hint schemas.
- `ssh_executor.py`: Wraps Paramiko SSH execution so the orchestrator can run remote prep, netlist, mapped, and GDSII commands on the server, with optional login-shell, preamble, PTY, and `srun` support.
- `exports.txt`: Local service environment exports loaded by `app.py` and the stage-specific runner scripts at startup, unless the parent shell already set those vars first.

Notes:

- Agent review and edit modules live in `agents/`.
- Worker orchestration and remote command construction live in `workers/`.
- `__pycache__/` contains generated Python bytecode and is not part of the service logic.
