# Forge 5.1.26

## Delivery recovery and update checks

External requests keep their original journal identity and permission scope.
Queue recovery proceeds in bounded steps, and each receive or maintenance tick
shares a limit for reading, classifying and admitting requests. Recipient order
is preserved while other eligible destinations can make progress.

The original journal remains authoritative. Derived queue data is checked before
use; unsupported or damaged data is preserved and channel delivery becomes
unavailable while local chat remains available. This change does not claim that
every part of application startup is bounded.

Goal requests save their accepted intent before preparing project files or
worktrees. An interrupted preparation pauses for inspection instead of starting
the same assignment again. Completed tool actions are not replayed. Existing
TODO documents and later edits or cancellation are respected; a filesystem that
cannot safely publish a new document causes preparation to pause.

Update recovery distinguishes a verified, fully recovered queue cache from
authoritative messages, runs, invocations and journal entries. Only supported
derived data can be excluded from rollback comparisons. Unknown SQL objects,
closed recovery and original data changes remain protected. Read-only checks
honor cancellation and deadlines and recognize SQLite's literal internal prefix
without overlooking similarly named user tables.

Queue tables are an additive private format. The global database schema stays
at version 7, and creating the tables requires a verified backup. Existing
rollback quarantine and inspection requirements continue to apply.

The preview retains the Library, chat focus and tray changes, drafts, keyboard
navigation, permission controls, HUD and existing warm palettes.

## Validation

Regression coverage includes recovery budgets, recipient ordering, crash
boundaries, cancellation, goal preparation races, legacy update comparisons,
unknown SQL objects, read-only snapshots and deadlines. Independent source and
disposable-data reviews passed. Final merged-source suites, packaged migration,
Windows lifecycle, installer and privacy checks remain required before release.
