# LEC retry results

Original run: `internal_200MHz_20261103_172729_848939`.
Experiment job: `733316`.

| Trial | Result | Elapsed time |
| --- | --- | --- |
| Exclude the width-mismatched `req_array_packed` match | 8 PASS, 53 FAIL; overall INCONCLUSIVE | 9 min 49 sec |
| Match top-level ports only; combine all outputs | TIMEOUT; overall INCONCLUSIVE; no counterexample produced | 10 min 17 sec |

The original run had 8 passing and 57 failing partitions. Four match obligations
disappeared when the suspect bus match was excluded; none of the remaining
partitions changed from failing to passing.

The second trial covered all 35 top-level output bits, with only top-level input
ports and no internal crosspoints. Its proof strategy reached its 600-second
limit. The elapsed time includes setup and finalization. No timeout was counted
as a pass or as a proven functional mismatch.

Both trials reused the original elaborated RTL and mapped-netlist models.
No RTL, library, reset assumptions, or production validator was changed.
Input hashes, coverage, and native statuses are recorded in `summary.json`.

The trials do not yet prove equivalence or a reachable functional mismatch.
The next useful experiment is a smaller set of output/subsystem partitions with
checked state matches and retained producer context, to avoid both arbitrary
internal states and a single expensive controller-wide proof.
