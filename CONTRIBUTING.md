# Contributing

Forge uses a shared Python backend with a Windows desktop host and a local browser host. Read [README.md](README.md) for setup, storage behavior and the limits of its project tools.

## Development environment

Use Python 3.12 on Windows for the desktop app and its complete test environment. From the repository root in PowerShell:

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
& ./.venv/Scripts/python.exe -m pytest -q
cd frontend
npm ci
npm test
npm run build
cd ..
```

The automated tests use temporary application-data directories and mock Ollama where appropriate. They should not require access to real conversations or projects. A passing mocked test suite does not validate model quality, native window behavior, GPU performance or container execution.

For interface changes, check the full workspace and HUD in the Windows app, including navigation, Stop, streaming, command approval and returning to the full view. For agent changes, add focused tests for the behavior and relevant failure cases. Use an isolated `FORGE_DATA_DIR` and a disposable project for manual checks.

Build the Windows application with `./build.ps1`. Preserve the generated `dist/Forge/_internal` directory when checking a portable build. Generated packages belong in release artifacts, not the source tree.

## Repository hygiene

- Do not commit credentials, `.env` files, local configuration, model files, application data, conversations, screenshots, logs or file backups.
- Keep examples generic. Do not include personal names, emails, account identifiers, machine-specific paths or details copied from real projects.
- Use synthetic fixtures and temporary directories in tests. Inspect output and attachments before sharing an issue or pull request.
- Review staged files and Git author metadata before publishing a commit. `.gitignore` does not remove data already tracked in Git history.
- Describe the concrete change, why it is needed and how it was checked. Distinguish automated coverage from live model, desktop and Docker verification.

## License

Forge is licensed under the [MIT License](LICENSE). By submitting a contribution, you agree to make it available under the same license. Preserve applicable copyright and license notices when incorporating third-party code or assets.
