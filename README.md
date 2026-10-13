<p align="center"><img src="assets/forge.svg" width="88" alt="Forge"></p>

# Forge 5.1.6

This Windows preview safeguards accepted Compatible and OpenRouter work across
tool rounds and restart, and simplifies the connection editor. It retains Forge's
warm chat layout, composer modes, temporary helper conversations and local tuning.
Read [the patch notes](docs/FORGE_516.md), [the chat and engine guide](docs/FORGE_514.md) and
[5.1 recovery guidance](docs/FORGE_51.md).

Your models. Your projects. Your workspace.

Forge is a local AI workspace for building apps, exploring ideas and working with
files on your computer. It brings chat, Builder, previews and project tools into
one Windows app. Optional OpenRouter guidance helps with planning and review.

**[Download the Windows preview installer](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.6/Forge-5.1.6-Setup.exe)**
· [Prerelease notes](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.6)
· [Portable ZIP](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.6/Forge-5.1.6-Portable.zip)
· [Publisher record](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.6/Forge-update-proof.json)
· [Checksum](https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.6/SHA256SUMS.txt)

The [previous 5.1.5 preview](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.5) remains available.

## Install

1. Install [Ollama](https://ollama.com/download/windows) and choose a local model.
2. Download **Forge-5.1.6-Setup.exe** and run it after closing Forge.
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

These images and animations use an isolated demo workspace with synthetic data.

![Forge composer with a selected goal workflow and an ordinary prompt](assets/demos/forge-5.1.4-goal-mode.png)

![Forge connection editor with primary controls and compact advanced settings](assets/demos/forge-5.1.6-engine-dark.png)

![Forge connection editor in the light workspace](assets/demos/forge-5.1.6-engine-light.png)

![Forge explaining a helper context pause while local work continues](assets/demos/forge-5.1.3-context-pause.png)

![Forge checking saved work before a verified restart](assets/demos/forge-5.1.3-restart-check.png)

![Forge chat with saved task activity, skills and reference notes](assets/demos/forge-5.1.2-workspace-dark.png)

![Forge connection setup with a verified tool preview](assets/demos/forge-5.1.2-connections-dark.png)

![Forge connection setup on a narrow light workspace](assets/demos/forge-5.1.2-connections-light.png)

![Forge workspace details scoped to the current chat](assets/demos/forge-5.1.2-activity.png)

<details>
<summary>Watch the workspace and Builder in action</summary>

Attach guidance for your next message:

![Selecting skills and reference notes in Forge](assets/demos/forge-5.1.2-context.gif)

Move from the brief to the preview and checks:

![Forge Builder brief, preview and checks](assets/demos/forge-5.1.2-builder.gif)

</details>

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
