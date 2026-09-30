# Remaining work for the full flow

1. **Verify merged backend mailbox support on the server.** Mailbox CLI, staging,
   provenance, top forwarding, and sweep reuse are present in the merged backend.
   The earlier [porting note](backend_mailbox_reapply.md) is retained as history.
2. **Mailbox publication implemented locally.** Successful frontend checks publish
   the complete RTL tree and matching expanded YAMLs to
   `shared/mailbox/<run-id>/<revision>/`, with file hashes and the existing RTL
   manifest/top. Selected MHz remains in run state. Repairs publish a new revision.
3. **Connect the validation node.** Transfer the selected revision to the server,
   initialize the required tool environment, run `server/validation/run_pipeline.py`
   over SSH, and retrieve reports. Require complete verification evidence;
   process exit zero alone is insufficient.
4. **Enforce the validation gate.** Backend must consume the exact revision that
   passed validation. Reject absent/stale evidence, including direct backend entry.
5. **Backend execution adapter implemented for explicit testing.** It passes the
   mailbox and target MHz, saves invocation-specific JSON/session reports, and
   checks completion plus input/target provenance. Direct testing requires
   `--allow-unvalidated`; connecting validated execution still depends on item 4.
6. **Connect failure handoffs.** Translate supported validation/backend RTL failures
   into the existing debug intake. Keep tool/environment failures separate. After
   repair, rerun frontend checks, publish a new revision, and validate again.
7. **Test the complete flow on the server.** Verify SSH, dependencies/tool setup,
   deployed prep support for directory input and `--top`, successful completion,
   and failure/repair routing.

Remote validation is intentionally deferred. The validation evidence gate,
normalized failure handoffs, and live server verification remain open. Backend
execution tests do not establish functional validation.
