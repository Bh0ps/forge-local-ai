# Build Windows .NET apps examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Find the real project and SDK before choosing a command

List the selected project first. Read only discovered solution/project files and existing SDK/build settings. These paths are placeholders for returned project paths, not new files to create. A .sln can contain C++ as well as managed projects; inspect its references before choosing this workflow.

Call `list_files`:

```json
{
  "path": ".",
  "recursive": false
}
```

Call `read_files`:

```json
{
  "files": [
    {
      "path": "<discovered .csproj>",
      "start_line": 1,
      "end_line": 160
    },
    {
      "path": "<discovered SDK/build settings file>",
      "start_line": 1,
      "end_line": 100
    }
  ]
}
```

Call `run_command`:

```json
{
  "argv": [
    "<SDK executable resolved by environment discovery>",
    "--info"
  ],
  "cwd": ".",
  "timeout": 20
}
```

Expected evidence: Resolve the executable with an offered environment/command tool, for example an approved Get-Command or where.exe query, then use its returned path. Record SDK versions and the applicable global.json selection, TargetFramework(s), UseWPF/UseWindowsForms, project references and OutputType. If the project is a library, do not label its DLL as the requested app. Inspect actual solution/project compatibility; never rename .sln to .slnx or retarget a framework to hide a missing SDK. Microsoft references: [global.json](https://learn.microsoft.com/en-us/dotnet/core/tools/global-json), [dotnet build](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-build).

Recovery: A missing executable or unsupported SDK is a prerequisite, not a successful build. Preserve NuGet feed configuration and local .user/.suo/.vs files. Do not publish credentials, machine paths or raw build logs, and do not install an SDK from a guessed URL. Follow an authorized setup path with the actual required version.

## Build the accepted solution and inspect the same managed session

The repository is confirmed SDK-style and its current SDK supports the selected solution format. Resolve the solution, configuration and SDK path before this example. For a legacy .NET Framework project, retain the discovered repository MSBuild command instead of forcing the dotnet CLI.

Call `command_start`:

```json
{
  "argv": [
    "<verified dotnet executable>",
    "build",
    "<actual solution or project path>",
    "--configuration",
    "<existing build configuration>"
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

Expected evidence: Retain terminal status, exit code, relevant diagnostics and emitted output paths. A build may perform package restore; respect feed/network approvals and existing locks. Follow the project's real test runner and arguments, including any SDK-specific runner selection; only use --no-build when the matching test output has actually been built. Record test counts and skipped tests; zero discovered tests is not a passing acceptance check. Avoid solution-wide shared output directories or invented bin paths. Reference: [dotnet test](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-test).

Recovery: While running, wait/read the existing session rather than launching another build. For a real compiler failure, identify the first relevant diagnostic and repair its source/reference boundary. A timeout or interrupted command may have written outputs: inspect session/invocation evidence before retrying. No completed action is replayed solely to obtain a nicer transcript.

## Deliver Visual Studio files and honest desktop acceptance evidence

The compiled app is ready for a representative startup and user-flow check. Identify its exact output, framework-dependent runtime or bundled dependencies, and an application-supported disposable data/config location before launch. There is no implied isolated native-desktop automation tool.

Expected evidence: Confirm that the actual .sln/.slnx and referenced .csproj files are present and use supported paths; report whether IDE opening was actually tested. Use a discovered hash utility or the project's manifest to record the real executable and dependency identity. A managed command is not a GUI screenshot or an input sandbox. Verify startup and the requested save/open/error flows only with an offered authorized tool or documented manual smoke; otherwise report GUI criteria unverified. Preserve generated designers/resources, existing visual conventions and local user settings.

Recovery: If disposable configuration is not supported or GUI verification is unavailable, retain the build/test evidence and explain the remaining criterion. Do not redirect HOME/LOCALAPPDATA and assume that native known folders are isolated. Keep required acceptance work open; do not fabricate an executable, IDE-open result or screenshot.
