# Backend mailbox changes to reapply

Date: 2026-09-29

## Status

The mailbox integration was implemented against older backend files. Those edits
need to be reapplied to the newer backend version; do not assume the newer files
contain them. This note preserves the intended behavior and implementation map.
At the time this note was written, the older edited files were still visible in
this workspace. Adapt the changes to the newer code rather than replacing newer
files with the old versions.

## Intended entry point

From the repository root:

```bash
python local/backend/app.py \
  --mailbox shared/mailbox/<run-id>/revision_001 \
  --target-mhz 200
```

`--mailbox` selects one explicit local revision containing a complete `rtl/`
directory. Backend uploads that RTL through its existing SSH connection and uses
the remote directory as the prep input instead of `MemoryController.zip`.
There is no need to create a ZIP for this mode. Omitting `--mailbox` retained the
existing standalone behavior in the original change.

The top-level orchestrator's configured frequency fallback is now 200 MHz.
It saves the selected target and should pass it explicitly to backend; this is
separate from changing backend's standalone timing defaults.

## Changes to port

| File | Change |
| --- | --- |
| `local/backend/app.py` | Add a `--mailbox` Path argument. Stage the selected mailbox after building SSH configuration and before calling `run_flow`. Merge the returned input path and provenance into the initial state. |
| `local/backend/services/mailbox_input.py` | Add `stage_mailbox(mailbox, project_root, ssh_config)` to validate, snapshot, hash, and upload the RTL tree. |
| `local/backend/schemas/state.py` | Add `mailbox_source: str`, `mailbox_revision: str`, and `mailbox_file_hashes: Dict[str, str]`. |
| `local/backend/workers/workers.py` | Keep using `remote_prepare_input` for prep's `--input`; for mailbox mode also forward the selected `top_module` through `--top`. |
| `local/backend/workers/README.md` | Document the mailbox CLI, layout, remote staging, and remaining validation responsibilities. |
| `local/backend/timing_closure/tests/test_mailbox_input.py` | Add mocked tests for staging, bad inputs, transfer failure, and sweep reuse. |
| `shared/top_orchestrator/nodes.py` | Use `backend_command(config, mailbox)` to build `app.py --mailbox <absolute-revision-path> --target-mhz <saved-value>`. This command builder does not itself connect the backend node. |

### Staging behavior

1. Require a real `rtl/` directory with at least one `.v` or `.sv` source.
   Reject symlinks and unsupported filesystem entries.
2. Copy all files beneath `rtl/` into a temporary local snapshot, preserving
   relative paths, headers, dependencies, and supporting files. Do not upload
   sibling `specs/` or report directories through this backend helper.
3. Compute SHA-256 hashes of the exact snapshotted file bytes and derive an RTL
   revision digest from the sorted path/hash mapping. This digest identifies the
   uploaded RTL bundle, not a validation result or the entire RTL-plus-spec bundle.
4. Read `rtl/manifest.json` when present and use its valid `top_module` field.
   The original implementation retained the existing configured top if absent.
5. Create a unique directory beneath the remote project's `mailbox_inputs/`, using
   the revision digest prefix plus a UUID. Preserve subdirectories and upload via
   `SSHExecutor.upload_file(..., exclusive=True)`. The existing transport helper
   already supported that API in the older code; verify it in the newer version.
6. Return `remote_prepare_input` pointing to that remote directory, plus the
   mailbox source path, revision digest, per-file hashes, and selected top when
   available. Normal backend state logging preserves these fields.
7. Propagate staging/upload errors so backend never starts on incomplete input
   or silently falls back to the legacy ZIP.

For `max clocking`, stage once per invocation and reuse that remote revision for
all frequency attempts. Carry the staged-input cache back from the per-target
argument copy to the parent invocation. Preserve the newer backend's sweep
start/step policy; mailbox support must not change it.

## What this did not implement

- Automatic frontend publication into revisioned mailboxes.
- Validation execution over SSH and collection of its reports.
- A gate proving that the exact RTL/spec revision passed validation.
- Execution inside the top-level `backend_node`, which remained a placeholder.
- Normalization of validation/backend failures into the frontend debug contract.

The intended lifecycle is: frontend working files -> mailbox snapshot ->
validation -> backend consumes that validated snapshot. Debug repairs the
frontend workspace; a repair produces a new snapshot and requires validation
again. Do not choose an arbitrary latest directory or treat successful upload
as validation success.

## Verification when reapplying

The original four mailbox tests and 21 top-level orchestrator tests passed with
SSH mocked. The local system Python lacked Paramiko, so backend tests used a
temporary in-memory Paramiko stub; no live upload or EDA run was tested.

Three existing CLI/max-clocking tests failed because tests expected 10 MHz steps
while the implementation used 5 MHz. The same failures were reproduced with
the pre-change backend app. Reassess against the newer version instead of
changing sweep behavior as part of this port.

Test that missing/empty RTL and symlinks fail before SSH, nested files and hashes
match uploaded bytes, upload errors prevent `run_flow`, mailbox metadata reaches
the initial state, and a sweep uploads only once. Run existing CLI and top-level
tests as well.

Before live integration, confirm the deployed remote
`prepare_rtl_for_genus_universal.py` accepts directory `--input` and `--top`.
Both were documented by the local prep instructions, but the remote script was
not present in this checkout and was not inspected.
