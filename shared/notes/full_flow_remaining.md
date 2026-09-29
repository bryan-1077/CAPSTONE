# Remaining work for the full flow

1. **Reapply backend mailbox support to the newer files.** See
   [backend_mailbox_reapply.md](backend_mailbox_reapply.md).
2. **Publish mailbox revisions.** Snapshot frontend RTL, matching YAML specs,
   and a manifest with file hashes, top module, and selected MHz into
   `shared/mailbox/<run-id>/<revision>/`. Publish a new revision after repairs.
3. **Connect the validation node.** Transfer the selected revision to the server,
   initialize the required tool environment, run `server/validation/run_pipeline.py`
   over SSH, and retrieve reports. Require complete verification evidence;
   process exit zero alone is insufficient.
4. **Enforce the validation gate.** Backend must consume the exact revision that
   passed validation. Reject absent/stale evidence, including direct backend entry.
5. **Connect the backend node.** Invoke the backend entry point with the validated
   mailbox path and saved target MHz, then collect and interpret its state/reports.
6. **Connect failure handoffs.** Translate supported validation/backend RTL failures
   into the existing debug intake. Keep tool/environment failures separate. After
   repair, rerun frontend checks, publish a new revision, and validate again.
7. **Test the complete flow on the server.** Verify SSH, dependencies/tool setup,
   deployed prep support for directory input and `--top`, successful completion,
   and failure/repair routing.

The individual subsystem flows exist. Mailbox publication, validation SSH, result
handling, and top-level routing/gates still need integration; SSH alone is not the
last missing piece.
