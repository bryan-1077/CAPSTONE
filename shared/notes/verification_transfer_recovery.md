Verification transfer recovery — November 4, 2026
================================================

Observed on `olympus.ece.tamu.edu`:

- `/mnt/nfs-scratch` is mounted from `olympus-strg-2:/storage/nfs-scratch`.
- Filesystem usage: 43%, with about 2.3 TB available.
- The latest validation workspace's `work` directory belongs to `antgamez1203`
  with mode `drwxrwsr-x`; ordinary mode bits permit owner writes.
- SSH commands and allocation queries work. SSH/Git processes remain in
  uninterruptible I/O wait (`D` state).
- A tiny, uniquely named SFTP upload under the project's `shared/` directory
  failed at the configured 10-second deadline. This reproduces the transfer
  problem independently of RTL generation and Innovus.
- No jobs for this account appeared in the latest allocation query.
- Cleanup of the isolated probe also timed out. If it was created, the only probe
  artifact is `shared/.codex_transfer_probe_4c4d99fa83344a5abb11993db0331dc5`
  under the remote project; it can be removed after storage recovers.

The exact server-side cause has not been established. Ask the server administrator
to investigate NFS service/client I/O and SFTP subsystem stalls on this mount.
There is no evidence that changing RTL, reinstalling Innovus, or clearing VS Code
forwarded ports will repair this transfer failure.

Suggested administrator report:

> On Olympus, transfers into `/mnt/nfs-scratch` are hanging before RTL verification
> starts. The mount is `olympus-strg-2:/storage/nfs-scratch`; disk usage is 43% and
> the workspace is writable by its owner. A tiny SFTP upload to a fresh file in
> `/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/` timed out, while
> ordinary SSH commands worked. SSH/Git processes are in D state. Please check
> shared-storage I/O and the SFTP subsystem on Olympus.

Client changes in this checkout:

- SFTP operations have a configurable deadline, default 120 seconds, covering
  subsystem startup, transfers, and flush/close. A deadline closes that SSH
  connection and reports the affected path.
- Validation prints per-file upload progress and explicitly announces when the
  server verification launcher starts. Transfer failures cannot become a PASS.
- Tests cover normal transfers, stalled startup, writes, flush, and downloads;
  47 SSH/validation tests passed.

These changes run in the local launcher; an existing running Python process will
not acquire them. After the administrator restores transfers, restart the normal
local flow using a fresh run/workspace. Retain the old run directories as evidence.
Do not repeatedly regenerate RTL while the same transfer failure persists.
