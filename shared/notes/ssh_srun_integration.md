# Reusing backend SSH and Slurm execution

## Implemented connection preflight

`python -m shared.top_orchestrator remote-check` now tests SSH followed by `srun`
environment setup. Connection settings come from exported local `CAPSTONE_*`
variables; `CAPSTONE_REMOTE_PROJECT_DIR` selects the actual remote directory.
An optional `--remote-config` JSON file overrides those values. See the
[setup instructions](../top_orchestrator/README.md#ssh-and-slurm-preflight).

Transport is extracted to [shared/remote](../remote/ssh_executor.py), with a backend
compatibility import. It drains both output streams, supports live output, and
enforces a command timeout. The preflight sets a Slurm time limit and attempts
cancellation by unique job name on failure/interruption. Validation execution,
RTL/YAML transfer, and report retrieval are now connected in shared/top_orchestrator/validation.py.
The deployed validation scripts remain unchanged; backend continuation is deferred.

## Execution model

Confirmed flow: connect to the login server over SSH, then launch `srun` there
to allocate compute resources and initialize the tool environment inside the job.
Run validation in that same job shell so it inherits the environment.

```text
Local orchestrator
  -> SSH login server
     -> upload revision via SFTP
     -> srun /bin/bash -lc '<environment setup && validation command>'
     -> retrieve this invocation's reports via SFTP
```

The backend's `_build_inner_ssh_command()` name is historical: its current
implementation uses `srun`, not another SSH hop. Do not copy the unused
`n01-zeus` default as a required destination.

## Existing code to reuse

| Source | Capability |
| --- | --- |
| [SSHExecutor](../../local/backend/services/ssh_executor.py) | Paramiko connection, login-shell commands, working directory, timeout, SFTP upload and download. |
| [_build_inner_ssh_command](../../local/backend/workers/workers.py) | Wraps tool commands in Slurm allocation and environment initialization. |
| [stage_mailbox](../../local/backend/services/mailbox_input.py) | Snapshots bytes, preserves relative paths, hashes uploaded files, and uses unique remote directories with exclusive uploads. Backend helper uploads only `rtl/`; validation also needs matching YAMLs. |
| [Backend app](../../local/backend/app.py) | Builds connection configuration and passes it to staging and execution. |

Backend execution from the top orchestrator already calls the local backend app,
which owns its SSH connection. Reuse this pattern for the validation adapter;
do not wrap the backend app in an additional remote invocation.

## Configuration to supply

As of October 8, 2026, the backend working directory on the Texas A&M server is
`/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/server/backend`.
Backend defaults use this directory for scripts, staging, and build outputs.
The SSH host remains `olympus.ece.tamu.edu`. The shared orchestrator's
`CAPSTONE_REMOTE_PROJECT_DIR` is a separate setting for the deployed repository
root; it should not point at `server/backend` when using repository-relative
validation paths such as `server/validation/run_pipeline.py`.

Keep these configurable rather than copying backend account-specific values:

- SSH host, port, username, and local private-key path.
- Remote working/project directory, validation script path, and staging directory.
  **The actual server directory may differ from the path hardcoded in backend.**
  Local repository paths do not establish remote deployment paths.
- Remote Python executable or virtual environment.
- Slurm job name, partition, QoS, CPU count, and execution timeout.
- Environment initialization commands needed by validation tools.

The current backend wrapper uses these arguments:

```text
srun --job-name=ecen-454 --cpus-per-task=1 \
     --partition=adademic --qos=olympus-academic /bin/bash -lc <quoted-job-command>
```

`adademic` is the literal spelling in the backend code. The user's confirmed
`load-ecen-454` alias itself runs `srun` with these partition/QoS settings plus
`--pty --x11=first bash -l`. It allocates an interactive job; it is not a tool
environment loader. Do not copy backend's conditional call to that alias into
the compute job: doing so would attempt a nested allocation. The top orchestrator
now runs one `srun`, starts login Bash, and sources `~/.bashrc` there. PTY/X11 are
not needed for the preflight. Validation may need additional tool setup:
behavioral simulation uses VCS, so Cadence tool availability is insufficient.

## Adapter implementation

1. Extract the transport into a shared module, keeping a compatibility import for
   backend. Keep Slurm/environment wrapping separate from generic SSH transport.
   Paramiko must be installed in the local Python environment running the adapter.
2. Upload the selected RTL/YAML snapshot into a pre-existing single-use workspace under `shared/`, selected by `validation_workspace` / `CAPSTONE_VALIDATION_WORKSPACE`. The orchestrator only checks directories; it never creates them. The validator still creates its own report/scratch directories, so execution is blocked unless `allow_validation_output_dirs` is explicitly true.
   Ensure the compute job can access that directory and the deployed validation
   code through the server filesystem.
3. Build a quoted job command that initializes the environment, checks required
   tools, changes to the configured working directory, and invokes the configured
   Python and `run_pipeline.py` with the remote input path. Setup failure must stop
   execution; do not silently continue with a missing tool environment.
4. Run that command through `srun` over the SSH connection. Preserve stdout,
   stderr, exit code, remote paths, and the input revision in local run artifacts.
5. Fetch reports from this invocation's directory. Require complete verification
   evidence for the selected inputs; exit zero alone is insufficient. Keep
   connection, allocation, environment, and report-transfer errors separate from
   RTL verification failures.

The shared SSH helper now drains both streams and streams output through an
optional callback. Its exception result uses `exit_code: -1`; distinguish that
from a remote process exit. Validation should reuse the preflight's bounded
allocation/job time and explicit cancellation pattern.

Only one layer should add `srun`: the transport already has optional `use_srun`
support, while backend embeds `srun` in its command builder. Do not enable both.
