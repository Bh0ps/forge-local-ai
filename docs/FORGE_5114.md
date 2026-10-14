# Forge 5.1.14 — project snapshots and stronger release checks

Project listing now reads project headers and their appearance records through
one read-only SQLite transaction. It closes the connection before decoding the
cosmetic records. The existing sorting, output fields, defaults and treatment of
orphan appearance records are preserved.

The previous implementation opened a new connection for each project's
appearance. Disposable fixtures returned identical project payloads with one
connection for 0, 1, 20 and 200 projects; the previous 200-project path used 201
connections. Reading both sets of records in one WAL snapshot also prevents a
concurrent appearance/header update from producing a mixed list.

Malformed appearance data falls back to the existing Folder icon. This changes
the coordinator's project-list query; the frontend layout and styling remain
those of the [previous refinement](FORGE_5113.md).

The native release smoke gate now requires a clean app exit as well as the
complete successful report. Previously, a valid report could pass after an
unsuccessful process exit. Controlled tests reproduced that gap for ordinary
errors and a Windows access-violation exit code. The fixed gate rejects them
with a bounded `process-exit` diagnostic and retains the report's status. The
original timeout and native workspace/browser/Builder requirements still apply.

SQLite storage fixtures showed that retired journal pages are reused and that a
held-reader WAL clears after the reader closes. No extra database maintenance
is needed for that observed behavior. Existing helper retirement and retained
activity summaries continue. Artifact-file retirement remains a separate,
unreleased investigation; ambiguous files are retained.
