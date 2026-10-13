---
name: windows-native-cpp
description: Build existing CMake or Visual Studio C++ projects with the selected compiler, configuration and dependencies.
license: MIT
---

# Build native C++ apps

## When to use

Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read CMakeLists/presets or .sln/.vcxproj, imports and dependencies. Preserve build system, toolset/SDK, architecture, locks, licenses and private user settings.
2. Discover compiler, CMake/CTest or MSBuild paths/versions. Use existing presets/configurations and documented developer environment; never guess installations, generators or SDKs.
3. Make hash-guarded source/build edits. Preserve includes, resources, exports, linking and references; generated projects/caches are not source.
4. Configure/build/test in the prescribed order with actual argv/cwd. Wait/read each managed session; require terminal status and discovered tests, without duplicate launches.
5. Identify real executable/dependencies/architecture/hash. Verify supported startup/GUI with disposable data and offered tools; working directories are not isolation.
6. Return project/preset/target, completed evidence and artifacts. Preserve private settings, obey approvals and report unmet prerequisites/checks.

## Verification and recovery

Separate configure/build, discovered tests, dependencies and runtime/UI evidence. Repair not_executed; inspect unknown/interrupted effects before retry. Never replay completed tools.

## Result packet

Project/preset; compiler/SDK/architecture/configuration; sessions/results; tests; artifact/dependency identity; smoke; limits.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
