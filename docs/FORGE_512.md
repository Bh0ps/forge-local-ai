# Forge 5.1.2 Windows preview

This release improves connection setup and the guidance that agents receive,
and establishes publisher authentication for future in-app updates. It keeps
Forge's colors, conversation layout and existing project workflows.

## Connections

MCP connections have a compact editor with a tool preview, an explicit sign-in
flow and clearer local-executable controls. **Test connection** checks an unsaved
draft without replacing the saved connection or writing its typed credentials.
Testing a local executable starts that server; its control says so.

**Save & enable** saves the connection disabled, verifies its saved configuration,
then enables that exact configuration. A delayed test or sign-in result cannot
enable a subsequently edited target. Cancelling ignores late responses and keeps
completed, explicitly saved changes. Credentials are reused only for the same
target and authentication binding. Tests that finish after shutdown cannot start
or publish a new connection.

Dialogs have accessible names, visible keyboard focus and focus restoration.
The editor fits narrow windows, enlarged text and light/dark themes.

![A connection test in an isolated demonstration workspace](../assets/demos/forge-5.1.2-connections-dark.png)

## More useful agent guidance

Six core workflows now include concrete tool-selection rules, output expectations
and examples: efficient tools, cloud collaboration, frontend implementation,
UI design, preview verification and code review. Short primary instructions fit
the existing context budget; longer examples are read when needed.

The catalog contains 24 workflows. Updating the starter library replaces only
unmodified Forge-owned files. Edited or deleted instructions and existing
selection preferences remain yours. Examples use registered tools and their
actual argument shapes, including verification evidence and helper recovery.

## Publisher-authenticated downloads

The package includes `Forge-build-proof.json`; the release includes
`Forge-update-proof.json`. An Ed25519 signature from a public key embedded in
Forge binds the repository, channel, exact version, source commit, installer,
portable archive and application files. GitHub metadata and checksums alone
cannot introduce a trusted publisher. Private signing material stays outside
the source and packages in the publisher's operating system credential vault.

Verification rejects expired or replayed release records, conflicting revisions,
unexpected files, unsafe Windows paths, links and junctions. The installed build
proof has no expiry; the release record has a bounded validity window. Invalid
or missing records leave a clear manual installer link.

These are **publisher-authenticated packages with Windows executables that do
not yet have an Authenticode certificate**. The Updates panel distinguishes
publisher authentication from a Windows certificate. Earlier previews cannot
bootstrap trust automatically; install this version manually from the reviewed
release. The installer preserves the complete old app folder before installing
a clean file set, so obsolete frontend chunks do not contaminate verification.
Chats, models and external Forge state stay in their existing locations.

Background GitHub detection and verified download preparation are supported.
**In-app apply and rollback are deliberately disabled in this seed release.**
The next handoff must confirm persistent drafts, reject unsaved transient work,
freeze edits and recheck coordinator idleness before restarting. No downloaded
installer is executed in the background by this version.

Maintainers can seal a clean committed build with `scripts/release_proof.py`.
Its private vault record must live outside the repository. The read-only
publisher-release workflow verifies the five public assets against the exact
tagged source without executing their hosts or using a signing secret in CI.

## Verification and recovery

The release gate covers backend and frontend suites, production browser checks,
native lifecycle, disposable installer upgrades and failure recovery, exact
package/source comparison and a private credential/path audit. Public demo media
uses fresh profiles and synthetic data; no inference or external account is used.
Observed counts are recorded in the GitHub release notes.

There is no new database migration. Schema-7 workspaces remain supported. Keep
the private app-folder backup and existing state backups until the upgrade is
verified. Earlier schema-6 applications require the restoration procedure in
the [5.1 recovery notes](FORGE_51.md#verification-and-recovery).
