# Computer-use selector

The default backend is local Laya. Jev is opt-in with external-state consent.

When the next UI step is selecting an observed control, use `jev_cua` with a
sanitized, scoped snapshot from the host browser/computer tool. Snapshot fields:
`surface_id`, `revision`, `text`, and `elements`. Each element has a unique `id`,
`label`, `role`, and `operations` (CLICK, TYPE_TEXT, or SELECT). SELECT also needs
an `options` map of observed option IDs to labels. Mark sensitive controls with
`sensitive: true`; remove private text from the goal and summary yourself.

This tool returns a proposal, never executes a computer action. Reobserve before
acting, check the snapshot hash/expiry, map IDs only to the host's current native
references, and use the normal approved computer tool. Unsupported or uncertain
steps return to reasoning. TYPE_TEXT requires the host to generate and check the
text. DONE requires independent verification of every goal requirement.

For multi-step automation without a generative call between clicks, use the
Python `jev_cua.run` host integration described in `docs/JEV-CUA.md`. Merely
calling this selector from a reasoning loop does not establish savings.
