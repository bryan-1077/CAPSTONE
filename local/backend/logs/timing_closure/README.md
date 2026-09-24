# Parsed Session Logs

Root files:

- `README.md`: description of the parsed output layout.

Stage files:

Each stage directory contains:

- `live_execution_transcript.log`: stage-specific live transcript and history lines.
- `stage_outputs.json`: candidate/output paths, output files, and directory-resolution metadata.
- `review_results.json`: reviewer pass/fail data, summaries, fixes, and routing fields.
- `edit_results.json`: edit/fix action summaries and applied action metadata.
- `executed_commands.log`: commands issued for that stage.
- `stderr.log`: collected stderr entries for that stage.
- `stdout.log`: collected stdout entries for that stage.
- `ai_payloads.json`: AI raw responses, request metadata, and AI review payloads.
- `structured_validation_metadata.json`: validation context, routing, review context, and parsed structured log metadata.
