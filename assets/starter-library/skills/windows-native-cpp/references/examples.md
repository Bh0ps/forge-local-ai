# Build native C++ apps examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Choose checked-in CMake presets instead of inventing a generator

The file listing identifies CMakeLists.txt and a checked-in CMakePresets.json. Read their relevant target/preset definitions and confirm the installed CMake version. A CMakeUserPresets.json contains local choices and must remain private and unmodified unless explicitly requested.

Call `read_files`:

```json
{
  "files": [
    {
      "path": "<actual CMakeLists.txt>",
      "start_line": 1,
      "end_line": 150
    },
    {
      "path": "<actual CMakePresets.json>",
      "start_line": 1,
      "end_line": 150
    }
  ]
}
```

Call `run_command`:

```json
{
  "argv": [
    "<discovered cmake executable>",
    "--version"
  ],
  "cwd": ".",
  "timeout": 20
}
```

Call `run_command`:

```json
{
  "argv": [
    "<same verified cmake executable>",
    "--list-presets"
  ],
  "cwd": ".",
  "timeout": 20
}
```

Expected evidence: Resolve the executable from actual discovery first. Choose a configure preset and matching build/test presets or binaryDir from the observed files and output. Preserve generator/toolset and per-configuration semantics; do not assume all presets or modern flags exist. Use the actual documented developer environment when required. References: [Visual Studio CMake presets](https://learn.microsoft.com/en-us/cpp/build/cmake-presets-vs), [CMake command-line manual](https://cmake.org/cmake/help/latest/manual/cmake.1.html).

Recovery: An unavailable compiler, preset or required dependency remains a named prerequisite. Do not replace project locks, copy another machine's cache or install a guessed SDK. Prefer the checked-in environment setup; private user presets and absolute machine paths are not release artifacts.

## Build an existing Visual Studio C++ project with its actual properties

The repository uses a .vcxproj and its solution, rather than CMake. Read the actual Configuration/Platform combinations, project dependencies, PlatformToolset and Windows SDK requirement. Resolve MSBuild and the required developer environment before this illustrative call.

Call `read_file`:

```json
{
  "path": "<actual .vcxproj>",
  "start_line": 1,
  "end_line": 180
}
```

Call `command_start`:

```json
{
  "argv": [
    "<verified MSBuild executable>",
    "<actual .sln or .vcxproj>",
    "/p:Configuration=<existing configuration>",
    "/p:Platform=<existing platform>"
  ],
  "cwd": ".",
  "max_seconds": 300
}
```

Call `command_wait`:

```json
{
  "id": "<returned command session id>",
  "timeout": 30
}
```

Call `command_read`:

```json
{
  "id": "<same command session id>",
  "start": 0,
  "limit": 6000
}
```

Expected evidence: For CMake, configure with the selected preset first, then use its real build command; do not apply this MSBuild example to generated project files. Read the actual emitted target path or target properties instead of guessing Debug/Release folders. Run the repository's registered tests or observed CTest configuration and require discovered tests, not just exit zero. Keep runtime-library and architecture choices coherent with dependencies. Microsoft reference: [MSVC tool discovery and builds](https://learn.microsoft.com/en-us/cpp/overview/acquire-msvc).

Recovery: Preserve the first relevant compiler/linker diagnostic and its source location. A missing library/export or wrong architecture needs an evidence-based dependency/configuration repair, not copying arbitrary DLLs from system directories. If compilation already completed, keep that evidence and do not replay it because a later runtime check failed.

## Separate build evidence from native application behavior

A target builds successfully but may still fail to start because its runtime or assets are absent. Identify the real output and dependency set, then use a documented disposable-data smoke or separately authorized manual/available-tool test.

Expected evidence: Record artifact hash/size from a verified utility or manifest, compiler/target architecture, linked runtime requirements, tests and actual startup observations. Confirm required resources and runtime dependencies from the package contract. Report whether a native GUI flow was exercised; browser preview tools do not verify a C++ desktop window. Do not claim a folder-only launch is isolated or that a background process proves an interactive app works.

Recovery: An uncertain launched process or write needs its owned session and durable outcome inspected; use command_stop only for an owned session when appropriate. Keep unrelated processes/user work untouched. If a missing tool prevents required UI verification, retain the concrete build/test evidence and the unresolved criterion.
