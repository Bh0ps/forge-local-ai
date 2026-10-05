# Verified updates and recovery

The current development preview is unsigned. It can check the fixed public
`Bh0ps/forge-local-ai` release feed, but automatic download and installation are
disabled until a verified publisher is compiled into a signed Forge build.
GitHub SHA256 digests establish byte integrity; they do not establish publisher
identity. No manifest, plugin, model response, MCP server or editable setting can
grant publisher trust. Install the first signed release manually after checking
its certificate, then later signed versions can use the updater.

The updater accepts stable versions only, the exact repository/tag/installer
asset URL, a GitHub SHA256 digest and bounded size. It follows only official
GitHub download redirects. A staged installer must also have a valid Windows
Authenticode chain, a timestamp, the configured publisher and the expected
product version. Apply repeats these checks. It verifies both installed Forge
executables after installation; package manifests also cover dependency files
and notices. Unsigned portable archives are never an automatic execution path.

## Apply and rollback

Pause active chats, agents, schedules and model operations, and resolve any
outcome-unknown actions. Forge quiesces new writers before preparing an update.
It records an operation ID, backs up the complete prior installation and makes
a consistent SQLite backup with configuration, attachments and goal Markdown.
Every saved file has a size and SHA256 record. Project folders, model downloads,
third-party plugins and Windows credentials remain in place.

A retained copy of the current executable runs the update helper after the
coordinator exits. The helper validates its operation, parent process identity,
paths, backup and package again. It never terminates an unrelated process. The
target is the exact per-user `Programs/Forge4` folder; linked paths, traversal,
reserved Windows names and executable plugin folders are rejected. A journal
prevents repeating a consumed operation. An interrupted `applying` operation
requires inspection rather than automatically running the installer again.

On an installer or signature failure, the helper retains failed bytes and
restores the prior installation, SQLite state, attachments and checkpoints.
After a successful update, **Rollback** explicitly restores that same snapshot
without executing an older downloaded installer. New application data is kept
in the backup before restoration. Project files changed since the snapshot are
not reverted. Backups remain under `.forge/backups/update-<operation-id>` until
the user removes them. Do not delete a backup while a rollback is pending.

If Windows has locked an installation or a backup was modified, the updater
fails with an actionable error. Inspect retained files; reinstall a verified
package manually if needed. Do not copy an unverified executable into Forge's
installation folder. Automatic updates require the native Windows installation;
source, browser and Docker clients use manual deployment.

## Diagnostics

**Export diagnostics** creates a local JSON artifact only. It includes Forge
and Python versions, Windows release/architecture, bounded numeric preferences,
permission/performance enums, object counts and update state. It excludes chat
content, prompts, attachments, IDs, project/model names, URLs, hostnames, user
paths, certificate identity, credentials and raw logs. Export does not upload or
message anyone. Inspect it before sharing. A raw log is not equivalent to this
redacted export.

## Build and clean-PC checks

`windows-ci.yml` tests Python/frontend, builds with exact hashed Python packages
and `npm ci`, runs the native lifecycle/isolated browser fixture, verifies a pinned
Inno Setup compiler, and uploads a clearly labeled unsigned preview. Actions are
pinned to full commit hashes; PR jobs receive read-only repository permission.
No PR receives signing credentials or release publication privileges.

The dependency lock targets Windows x64 and CPython 3.12. Update it deliberately
using `scripts/release_lock.py` after validating the new versions. ZIP entry times
and ordering are stable; Windows PE/signature timestamps and PyInstaller builds
are not claimed to be bit-for-bit reproducible.

For a clean Windows 10/11 x64 VM or Windows Sandbox account with no Forge data:

1. Obtain the release assets and compare the installer SHA256 with the release.
2. Run `scripts/release_clean_pc.ps1` with installer, checksum and a new output
   folder. Explicitly use `-AllowUnsignedPreview` only for the unsigned preview.
3. Confirm first-run **Always Ask**, no model download until accepted, native
   browser isolation, full/HUD mode, close-to-tray and Quit.
4. Check WebView2 availability, 100/150/200% DPI, keyboard navigation, microphone
   setup, dictation with a downloaded local model, browser extension disconnect,
   network-offline error recovery, upgrade/rollback and uninstall.
5. Record Windows build and results. Keep all generated state outside Git.

WebView2 and the Windows desktop runtime remain prerequisites; the native smoke
fails visibly when they are absent. Hosted CI is a build smoke, not a substitute
for this clean Windows VM acceptance check. No clean-VM or signed-release result
is claimed until it has actually been recorded.
