# Gold Engineering — review drafts

Open `index.html` locally. Choose one of three visual directions and one of four
screens: Active Run, Waiting for Approval, Results, or Files. The gallery controls
work; the application controls inside the drawings are illustrations.

These drafts are separate from the running GUI. They contain representative,
illustrative data and static chip artwork. Run names are abbreviated in narrow
sidebar cards for the mockups. This is a visual proposal, not a proposal to change
orchestrator behavior, approval gates, run naming, or worker lifecycle. The existing
New Design guard, Active Run return action, and inline specification editing should
remain in the actual application. Required user input must remain prominent while
browsing a different run.

## Directions

- **A — Blueprint + Brass:** existing blue foundation with brass contacts, selected
  navigation underlines, modest corner rounding, and cleaner panel hierarchy.
- **B — Precision Console:** tighter geometry, numbered stages, a gold header rule,
  and more instrument-like framing. Could be too severe for the full application.
- **C — Engineering Studio:** lighter slate panels, softer corners, and a more
  approachable surface treatment. Useful as a source of panel and corner treatments.

Recommended mix: A's palette and PCB identity; C's softer surfaces; B's stage
numbers if useful. Avoid using gold outlines on every panel. Gold should identify
primary actions, selected navigation, inspected stages, and a few board contacts.
Cyan identifies activity, green identifies success, orange identifies input waits,
and red identifies failures. Every status also needs text and an icon.

A practical initial type scale is 26–28 px branding, 20–22 px page titles,
16–18 px section titles, 14 px body text, and 12 px metadata. Keep most labels
above the small sizes used for illustrative annotations here. Use a spacing scale
of 4, 8, 12, 16, and 24 px and mostly 6–8 px corner rounding. The 1600×1000 SVG
coordinates are an illustration canvas, not fixed dimensions for Qt layouts.

## Future portability

Translate the chosen direction into Qt layouts and application-owned vector icons.
Use text measurements, minimum sizes, wrapping, splitters, and logical pixels
rather than positioning production widgets using these SVG coordinates. Bundle
fonts only with appropriate redistribution licenses and preserve platform fallbacks.
Provide visible keyboard focus, tooltips for elided names, and sufficient contrast
when a selection loses focus. Check 100%, 150%, and 200% scaling and small restored
windows on Linux, WSLg, and macOS. These drawings are not evidence of compatibility;
macOS/native Windows execution still needs its separate worker support.

## Artifacts

- `comparison.png` compares the three Active Run drafts.
- `a-running.png`, `b-running.png`, `c-running.png` are full-size previews.
- `a-approval.png`, `a-results.png`, `a-files.png` illustrate the recommended family.
- Twelve SVGs provide scalable versions of every style/screen combination.
- `build_drafts.py` regenerates the gallery and previews with PySide6's SVG renderer.

Regenerate from the repository root:

```bash
QT_QPA_PLATFORM=offscreen shared/gui/.venv/bin/python shared/gui/design_drafts/gold_engineering/build_drafts.py
```
