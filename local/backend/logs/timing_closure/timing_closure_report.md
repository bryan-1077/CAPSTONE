# Setup timing closure: failed

GDSII review failed after 1 iterations: GDSII output directory does not contain an expected layout artifact (*.gds or *.gdsii).; GDSII generation command failed with exit code 1: srun: job 644297 queued and waiting for resources
srun: job 644297 has been allocated resources
Traceback (most recent call last):
  File "/home/ugrads/a/antgamez1203/capstone/.timing_closure_runner_200MHz_01.py", line 1673, in <module>
    raise SystemExit(main())
                     ^^^^^^
  File "/home/ugrads/a/antgamez1203/capstone/.timing_closure_runner_200MHz_01.py", line 1657, in main
    run_bash(bash_cmd, logfile=wrapper_log, cwd=outdir)
  File "/home/ugrads/a/antgamez1203/capstone/.timing_closure_runner_200MHz_01.py", line 35, in run_bash
    raise RuntimeError(
RuntimeError: Command failed with exit code 7. See log: /home/ugrads/a/antgamez1203/capstone/build_GDSII_200MHz_01/logs/run_wrapper.log
srun: error: n01-zeus: task 0: Exited with exit code 1; GDSII stage did not complete successfully: wrapper raised RuntimeError after underlying Innovus run returned non-zero (wrapper exit code 7; stage rc=1). No *.gds/*.gdsii was produced.; Failure is GDSII-owned (post-launch tool execution): Innovus was found, licensed, and began importing LEF/lib/netlist/MMMC/SDC; the mapped handoff contract appears present (mapped .v/.sdc/.db).

Run: `target_200MHz`


Limits: die footprint <= 4 mm²; reported total power <= 2 W.

Attempt 1 (preset): implementation_failed; WNS None; measurements {}
