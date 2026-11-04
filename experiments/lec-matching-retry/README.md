# LEC matching experiment

Source run: `internal_200MHz_20261103_172729_848939`.

The runner reuses the original EQY `gold.il` and `gate.il` models, after their
original elaboration, memory mapping, and clock conversion. It records their
SHA-256 hashes. It does not edit RTL, mapped netlists, libraries, reset behavior,
or the deployed Agent 4 checker. Results are diagnostic and do not replace the
original verification receipt.

Two trials run sequentially on a six-CPU Slurm job:

1. `width_checked`: exclude internal matches whose width, offset, or direction
   differs in the saved models. The detected mismatch is `req_array_packed`
   (204 reference bits versus four implementation bits).
2. `closed_outputs`: match only the top-level ports and group all outputs into
   one partition. Binding the matched nets keeps producer logic inside the
   comparison instead of treating internal signals as independent inputs.

Every top-level output remains covered. Both trials retain the original
`abc pdr` engine and depth 20. Each strategy has a 600-second limit; the runner
has a 720-second wall limit per trial, including setup. A timeout is unresolved,
never a pass.

The configuration uses native EQY [matching and collection rules](https://yosyshq.readthedocs.io/projects/eqy/en/latest/config.html).

Upload `runner.py` into a new, exclusive remote directory and invoke
`python3 runner.py --submit`. Each trial saves `proof.eqy`, `eqy.log`, its EQY
work directory, and `result.json`. The parent directory saves `manifest.json`,
`job_id.txt`, and `job.log`.

Current remote experiment directory:
`/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/lec_matching_20261103_01`

Slurm job: `733316`.

## Results

The width-check trial completed in 589.39 seconds without a timeout: eight
partitions passed and 53 failed. The four fewer failed partitions are the
excluded `req_array_packed` match obligations, not four newly proved checks.

The closed-output trial generated one partition covering `cmd_ready`,
`txn_ready`, `rsp_valid`, and all 32 `rsp_rdata` bits. Its only inputs are the
seven top-level input ports; it has no internal crosspoints. It reached the
600-second strategy limit without producing a proof or counterexample. Total
trial time, including setup, was 616.76 seconds. Its verdict is INCONCLUSIVE.

Downloaded evidence is in `results/`, with a machine-readable summary in
`results/summary.json`. Run `python3 report.py results` to regenerate the summary
and verify complete output coverage and the absence of internal proof inputs.

Neither trial establishes equivalence. The width mismatch alone did not resolve
the remaining failures. Removing all internal cutpoints traded the original
local counterexamples for a harder proof that timed out. This does not establish
whether the original counterexamples were reachable. A next experiment would
use smaller output/subsystem groups with checked state correspondences and
retained producer context, instead of one combined controller-wide cone.
