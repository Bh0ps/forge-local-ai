---
name: windows-dotnet
description: Build C# WPF or WinForms applications and usable Visual Studio projects with the existing toolchain.
license: MIT
---

# Build Windows .NET apps

## When to use

Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read .sln/.slnx/.csproj, SDK pin, build properties and entry point. Preserve WPF/WinForms, designers, references, locks, notices and private IDE settings; keep the native stack.
2. Discover SDK/MSBuild paths and versions. Match framework/Windows requirements; retain legacy MSBuild when required. Report missing prerequisites; never guess installations or retarget to hide failure.
3. Read affected source; make minimal hash-guarded edits. New projects use observed templates only when requested.
4. Build/test with actual argv/cwd. Keep command_start's ID; wait/read the same session. Require terminal status and discovered test counts.
5. Identify real app outputs/dependencies/hash. Check startup/GUI only with supported disposable configuration and offered tools; a folder is not isolation. Mark unperformed checks.
6. Return usable Visual Studio paths, toolchain, sessions, tests and artifacts. Keep secrets/private logs out; obey coordinator approvals and verification.

## Verification and recovery

Separate compilation, discovered tests, startup and GUI evidence. Correct not_executed; inspect unknown/interrupted effects before retry. Keep completed evidence/IDs.

## Result packet

Projects; SDK/framework/configuration; sessions/results; tests; artifacts/dependencies/hash; disposable smoke; limits.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
