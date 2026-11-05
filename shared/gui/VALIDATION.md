# GUI validation

Validation date: 2026-11-05. All 19 offline tests passed. Offscreen startup loaded 28 historical runs. The launcher also passed diagnostics when invoked from outside the repository.

## Completed on development Linux machine

- [x] Inspect local Python and pinned direct dependency versions.
- [x] Launch the real window through the offscreen Qt plugin.
- [x] Discover and render 28 existing run directories in the smoke check.
- [x] Test malformed state, moved artifacts, bounded log previews, and incomplete evidence.
- [x] Test candidate revision, digest-bound approval, and stale-response rejection.
- [x] Test private socket responses without credential files.
- [x] Test credential callback and cleanup of cached credential references.
- [x] Test detached-client reconnection to a fake worker with pending input.
- [x] Test cancellation of the fake worker and its local child process.
- [x] Test Keep Window Open as default and Leave Run Executing & Close behavior.
- [x] Test process start identity mismatch and conservative unknown remote cleanup.
- [x] Run the actual orchestrator router through GUI adapters using offline stages.
- [x] Confirm failed verification blocks backend and explicit skips retain routing.
- [x] Confirm test credentials do not appear in saved orchestrator state.

```bash
./shared/gui/launch.sh --doctor
./shared/gui/launch.sh --smoke-test
python3 -m unittest discover -s shared/gui -t . -p 'test_*.py'
```

Socket tests require permission to bind local Unix sockets. Tests use temporary
files and fake workers; they do not call models, SSH, Slurm, or EDA tools.

## Native desktop and configured execution

- [ ] Check window sizing, scrolling, readable text, and keyboard navigation.
- [ ] Open a historical report and run directory in their native applications.
- [ ] Create a fresh virtual environment and install pinned dependencies.
- [ ] Check provider configuration, local Verilator/build tools, and approved YAML.
- [ ] Select an account-specific remote configuration; confirm server PDK and EDA setup.
- [ ] Run one full Standard flow and record its directory and resulting evidence.
- [ ] Confirm live stages, repair attempts, logs, LEC, and final validation outcomes.
- [ ] Close the window with Leave Run Executing & Close; reopen and reconnect.
- [ ] Repeat reconnection while specification approval and credentials are pending.
- [ ] Stop during a local stage and confirm descendants terminate.
- [ ] Stop while queued for a Slurm allocation and while a remote stage executes.
- [ ] Independently verify owned remote allocation disappearance in the queue.
- [ ] Simulate lost SSH connectivity; retain cleanup_unconfirmed and job identifiers.
- [ ] Check desktop logout behavior separately from closing the application window.

## Second Linux computer

- [ ] Clone the repository without copying local virtual environments or credentials.
- [ ] Create the GUI environment and configure tools and account-specific settings.
- [ ] Run diagnostics, smoke check, and the offline suite.
- [ ] Open a copied historical run and confirm rebased in-run artifact paths.
- [ ] Repeat configured execution and close/reconnect checks.
- [ ] Record OS, Python, Qt version, installation issues, and validation evidence here.

## Packaging gate

- [ ] Complete native desktop, clean-install, and second-machine checks.
- [ ] Choose supported Python/Linux versions from the results.
- [ ] Lock transitive dependencies and review Qt distribution notices.
- [ ] Add a desktop shortcut or application package using the verified launcher.
