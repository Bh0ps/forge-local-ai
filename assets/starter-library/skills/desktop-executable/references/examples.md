# Package a desktop executable examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Use the repository's packaging contract and fresh outputs

The selected project already has a release script or .NET publish profile. Read its real build/package commands, output policy, dependency locks and licensing. A personal .pubxml.user/.env or saved application workspace is never a packaging input to disclose.

Call `read_file`:

```json
{
  "path": "<discovered packaging script or public publish profile>",
  "start_line": 1,
  "end_line": 180
}
```

Call `command_start`:

```json
{
  "argv": [
    "<verified repository release runner>",
    "<existing release script>",
    "<actual documented packaging arguments>"
  ],
  "cwd": ".",
  "max_seconds": 600
}
```

Call `command_wait`:

```json
{
  "id": "<returned packaging command session id>",
  "timeout": 30
}
```

Call `command_read`:

```json
{
  "id": "<same packaging command session id>",
  "start": 0,
  "limit": 6000
}
```

Expected evidence: Resolve placeholders into the real argv array before execution; preserve runner conventions, exact source and approved output path. For .NET, use the actual app project and declared publish profile/runtime mode; record whether the package needs an installed runtime. Avoid new trimming, AOT or single-file flags without compatibility evidence. No unrelated state or private credentials go into the artifact. References: [dotnet publish](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-publish), [single-file deployment](https://learn.microsoft.com/en-us/dotnet/core/deploying/single-file/overview).

Recovery: A CLI error or incomplete output is not a release. Inspect the first packaging diagnostic and the same session; keep older artifacts intact. Signing keys, uploads and installer replacement need the user's authorized delivery workflow and are not implied by these build examples.

## Retain an existing PyInstaller spec and its project interpreter

The repository is confirmed Python/PyInstaller and its locked project interpreter has the required package. Read the existing spec, entry point and release script first. Use this direct example only when the repository actually supports it; the script remains authoritative when it wraps additional steps.

Call `run_command`:

```json
{
  "argv": [
    "<verified project Python interpreter>",
    "-m",
    "PyInstaller",
    "--version"
  ],
  "cwd": ".",
  "timeout": 20
}
```

Call `command_start`:

```json
{
  "argv": [
    "<same verified project Python interpreter>",
    "-m",
    "PyInstaller",
    "<actual existing .spec>",
    "--distpath",
    "<new scoped output directory>",
    "--workpath",
    "<new scoped work directory>"
  ],
  "cwd": ".",
  "max_seconds": 600
}
```

Expected evidence: Preserve the spec's entry point, data files, hidden imports, runtime hooks, one-folder/one-file and console/windowed choices. Do not mix arbitrary global Python environments or assume every CLI flag overrides a spec. Inspect the actual output inventory and checksums, notices and dependency identity. Source documentation: [PyInstaller spec files](https://pyinstaller.org/en/stable/spec-files.html). A successful --version or packaging exit does not verify startup or GUI behavior.

Recovery: If the package or dependency is missing, report the exact lock/environment prerequisite and use an authorized setup path. Do not auto-upgrade it or substitute another interpreter. Keep secrets and application state out of the bundle; preserve temporary/published outputs when execution outcome is unknown.

## Verify real artifacts without using personal application state

Packaging completed with a concrete artifact inventory. Verify the actual files and perform only a documented safe startup/flow check using synthetic input and supported disposable configuration. If the app cannot direct writes away from personal state, report the test limitation.

Call `artifact_read`:

```json
{
  "id": "<returned build or verification artifact id>",
  "start": 0,
  "limit": 6000
}
```

Expected evidence: Use a discovered checksum/signature utility or the project's checker to confirm identity; do not read an EXE as UTF-8 or fabricate a hash. Record platform, source/configuration, path, hash, size, dependencies/notices, privacy review and actual smoke observations. Signed status requires real signature verification. A web screenshot does not prove a desktop EXE flow; tests and process startup are separate evidence. Upgrade/rollback and publication remain separate acceptance actions.

Recovery: Read existing evidence before repeating a completed build. For outcome_unknown, inspect durable invocation/session and filesystem evidence before a new side effect. Retain backups and partial outputs, disclose only sanitized diagnostics, and keep required unverified criteria open instead of claiming success.
