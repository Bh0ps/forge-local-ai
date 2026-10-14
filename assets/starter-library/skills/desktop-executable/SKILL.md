---
name: desktop-executable
description: Verify packages through the existing .NET, PyInstaller, Tauri or release pipeline.
license: MIT
---

# Package a desktop executable

## When to use

Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read release scripts, .spec/publish profile, Cargo/Tauri config, entry point, locks and notices. Preserve the stack/pipeline; building does not authorize signing, publishing or replacing an installation.
2. Discover runners/versions/flags, architecture/runtime/outputs. No guessed SDK paths, new trimming/AOT/single-file choices or private profile rewrites.
3. Build intended source into fresh scoped output with actual argv/cwd. Wait/read owned sessions and terminal results; preserve earlier artifacts/user data.
4. Verify actual executable, assets/dependencies/notices and hashes. Exclude secrets, personal state/logs, models/caches and IDE settings; existence is not verification.
5. Check supported startup/GUI with disposable configuration and authorized tools; folders are not sandboxes. Requested upgrade/rollback preserves backups.
6. Return artifact/runtime identity, checks and untested limits. Separate build/startup/GUI/signature evidence; follow approvals.

## Verification and recovery

Verify artifact/dependency/privacy facts separately from startup/UI/signature evidence. Keep completed outputs/backups. Inspect unknown effects before retry; prerequisites/consent need their own recovery.

## Result packet

Source/configuration/toolchain; sessions/results; artifact path/hash/size; dependencies/notices/privacy; disposable smoke; delivery limits.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
