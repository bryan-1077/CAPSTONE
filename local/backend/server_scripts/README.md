This directory contains the user's supplied server Innovus runner with automatic
checkpoint screenshot export added. It preserves the supplied script's physical
design behavior. The newer runner in `../probes/run_innovus_GDSII_universal.py`
has the same screenshot support and retains its existing timing and physical
verification features.

Copy `run_innovus_GDSII_universal.py` from this directory to the server to update
the supplied standalone script. For runs launched through this repository's
backend, its orchestrator uploads the runner from `probes/` instead.

Run on the server using the usual arguments:

```bash
python3 run_innovus_GDSII_universal.py --mappeddir build_mapped_mc --outdir build_GDSII_mc
```

After successful GDS generation, screenshot export runs in three fresh Innovus
GUI sessions. Implementation continues to use `-no_gui`. Output files:

```text
build_GDSII_mc/results/screenshots/floorplan.png
build_GDSII_mc/results/screenshots/placement.png
build_GDSII_mc/results/screenshots/routing.png
build_GDSII_mc/results/screenshots/manifest.json
```

The source checkpoints are `db/02_powerplan.enc`, `db/03_place.enc`, and
`db/06_final.enc`, respectively. Each loader needs its accompanying `.enc.dat`
directory and access to the original PDK/library files. Each stage uses a full
design view: floorplan, placement, and physical routing. The PNG is the layout
canvas, without desktop/window decorations. Actual image dimensions are recorded
in the manifest; the virtual display is 1920 by 1080, while the canvas may be smaller.

The repository runner records `screenshot_lineage.json` for every build, including
when screenshot export is disabled. Timing/physical recovery builds follow this
history to find floorplan and placement checkpoints in their source build; those
images are explicitly labeled `origin: ancestor_build` with `source_build` and
the actual checkpoint path in the manifest. They document stages from the
ancestor build. Routing always uses the current build's
`06_final.enc`; a missing routing checkpoint remains an error. Preserve ancestor
builds until their stage images have been exported. For older recovery builds,
the exporter can read the literal `set eco_checkpoint {...}` restore statement
from their generated `tcl/run_innovus.tcl` without executing it for discovery.

By default, `--screenshots auto` prefers `xvfb-run` when available, avoiding stale
SSH `DISPLAY` values; otherwise it uses the current `DISPLAY`. A headless server
needs Xvfb, xvfb-run, and xauth available. No packages
are installed by the script. Force virtual display with `--screenshots xvfb`,
use an existing/X-forwarded display with `--screenshots display`, or disable
exports with `--screenshots off`. Run inside the server's usual allocation and
license environment. Export needs the same Innovus libraries and license access.

Retry screenshots from an existing build without repeating implementation:

```bash
python3 run_innovus_GDSII_universal.py --screenshots-only --outdir build_GDSII_mc --screenshots xvfb
```

If an older recovery build has no recorded source history, explicitly name the
full build whose checkpoints it inherited:

```bash
python3 run_innovus_GDSII_universal.py --screenshots-only --outdir build_GDSII_recovery --screenshot-source-build build_GDSII_original --screenshots xvfb
```

Screenshot-only mode does not require mapped inputs or PDK discovery. It returns
nonzero if any image fails. Full implementation mode retains successful GDS
output and warns if screenshots fail. Inspect `results/screenshots/manifest.json`
and `logs/screenshot_<stage>.log`; native Innovus logs and generated Tcl scripts
are also retained. The default timeout is 300 seconds per image; increase it with
`--screenshot-timeout 600` for large designs or license waits.
Failed entries include the last 6 KB of console output in `log_tail`, and launched
entries include the actual command. Tcl failures include a stack trace and stage
markers. A failed startup still needs diagnosis from these logs; choosing a fresh
virtual display does not repair missing graphics libraries or a license failure.

PNG validation checks structure, checksums, and minimum dimensions. Visual
inspection on the server is still required to confirm the layer/view appearance;
local tests use a Tcl/Innovus test double and do not establish compatibility with
the installed Innovus release. The Tcl uses `setDrawView`, `setLayerPreference`,
`fit`, `redraw`, and `dumpToPNG`; required-command errors fail visibly in the
manifest, while unsupported individual layer preferences produce warnings.
The export command is documented in this
[Cadence discussion](https://community.cadence.com/cadence_technology_forums/f/digital-implementation/42850/createsnapshot-for-an-exact-bbox).

Local validation:

```bash
python3 -m unittest discover -s local/backend/probes/tests -v
```
