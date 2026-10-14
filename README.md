<p align="center"><img src="assets/forge.svg" width="88" alt="Forge"></p>

# Forge 5.1.26

This Windows preview improves delivery queue recovery and update checks while
retaining the clearer Library, chat focus and tray fixes. Requests keep their
original scope and completed work; an uncertain preparation pauses for review.
Compact workspace tabs, grouped composer, searchable Settings, Forge's font and
warm palettes are preserved.
Desktop actions share your Windows focus and pointer; this preview does not
provide an isolated desktop. Read [the patch notes](docs/FORGE_5126.md),
[the computer-tool guide](docs/FORGE_5110.md), [the chat and engine guide](docs/FORGE_514.md) and
[5.1 recovery guidance](docs/FORGE_51.md).

Your models. Your projects. Your workspace.

Forge is a local AI workspace for building apps, exploring ideas and working with
files on your computer. It brings chat, Builder, previews and project tools into
one Windows app. Optional OpenRouter guidance helps with planning and review.

**[Download the Windows preview installer](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.26/Forge-5.1.26-Setup.exe)**
· [Prerelease notes](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.26)
· [Portable ZIP](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.26/Forge-5.1.26-Portable.zip)
· [Publisher record](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.26/Forge-update-proof.json)
· [Checksum](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.26/SHA256SUMS.txt)

The [previous 5.1.25 preview](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.25) remains available.

## Install

1. Install [Ollama](https://ollama.com/download/windows) and choose a local model.
2. Download **Forge-5.1.26-Setup.exe** and run it after closing Forge.
3. Open Forge and follow the setup wizard to connect your model.

Windows 10/11, 64-bit, with WebView2 is required. Python, Node.js and the source
repository are not needed. The installer creates shortcuts and updates the app
while preserving your local chats, projects and model selections. The 5.1 state
migration makes a backup; read the [recovery notes](docs/FORGE_51.md#verification-and-recovery) before reverting to an older build.

The Windows executables do not yet have an Authenticode certificate. Forge's
independent publisher signature binds the installer, portable archive, source
commit and complete application file set. The first installation from an older
preview is manual. Forge checks GitHub automatically and verifies downloads.
In-app installation confirms saved drafts and blocks on unfinished work before
an authenticated helper restarts Forge. Restore uses a verified previous backup.
Ollama and model downloads are separate. No cloud account is required.

## Make it your own

- Talk to a local model and connect the project folders you choose.
- Choose a slash workflow as a composer mode while keeping your prompt readable.
- Start with **`/builder`**, shape a brief, then use **`/build`** to implement it.
- Keep the app preview, changes and checks alongside the work.
- Attach skills and reference notes to guide the next message.
- Inspect saved tool steps, helper attempts and their actual serving models.
- Keep helper results in parent activity without extra temporary conversations.
- Compare supported thread settings while preserving the selected model and context.
- Choose workspace accents and project icons, and resize or collapse your panes.
- Pause and resume tracked work with saved checkpoints.
- Use the compact HUD when you want Forge close by.

## A look inside

These Library screenshots use an isolated workspace with synthetic data.

![Library filters, skill provenance and outlined controls in the dark workspace](assets/demos/forge-5.1.26-library-dark.png)

![Readable skill instructions in a narrow light window with text enlarged to 150%](assets/demos/forge-5.1.26-library-instructions-narrow-light.png)

## Connect a browser or Telegram

The [Chrome and Edge extension](browser-extension/README.md) lets you explicitly
connect a tab, see its connection status, disconnect or reconnect. Forge's built-in
browser and local Builder preview are also available without the extension.

For Telegram, open **Settings → Connected workflows**, add your bot, connect and
pair your account. Enable **Include response content** to receive the answer and
continue the conversation. Keep Forge and your computer running.

Use `/model` to see your selection, `/model list` to see available models, and
`/model <name or number>` to switch. `/reasoning` shows the current setting;
`/reasoning on` or `/reasoning off` changes it where the model supports that control.
Use `default` with either command to return to the configured defaults. Settings
are saved for that Telegram chat and apply to its next request or safe resume.

Start with **Always Ask** and approve actions you understand. Credentials stay in
the OS vault. Optional OpenRouter helpers use free routes with no paid fallback.

## Develop from source

Forge uses Python 3.12, React/TypeScript, SQLite and WebView2. Use Node.js 24.18.0.
See [Contributing](CONTRIBUTING.md) for setup and tests; `build.ps1` builds the
Windows app. Generated packages, private workspaces and local evidence stay out
of the source repository.

[MIT license](LICENSE) · [Security policy](SECURITY.md)
· [Third-party notices](THIRD_PARTY_NOTICES.md)
