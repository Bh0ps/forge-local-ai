# Sidekick quick start

Follow the [installation instructions](README.md#run-the-windows-app-from-source), then start Ollama and Sidekick.

1. Select an installed model. Coding actions require tool support; screen captures require vision support.
2. Open **Projects → Add folder** and choose your code folder.
3. Ask for a concrete task. For example: “Inspect the project, create a short task list and fix the first issue.”
4. Expand tool cards to inspect arguments, results, diffs and backups.
5. Review proposed commands and choose **Run once** or **Deny**.

## Full workspace and HUD

The full workspace shows the conversation, project navigation, model selector and settings. Click **HUD** or press **Ctrl+Shift+H** for a compact, always-on-top chat bar. Both views keep the same conversation and project.

In the HUD, use **Show reply** to reveal the response area, click the project badge to open workspace navigation, or choose **Expand** to restore the full view. Command approvals open in the full view for review.

Enter sends a message; Shift+Enter adds a line. The send button becomes **Stop** during a response. Supported models can stream reasoning; control this with **Settings → General → Show model reasoning**.

## Context and saved work

**Settings → General → Context window** provides a 2K–256K token slider in 2K steps, with a 32K interface default. Changes persist and apply to the next prompt. The window includes history, tools and reply; small windows may not fit project tools. Larger windows require more memory and must fit the model's reported context limit.

Chats save automatically. Open **Projects → Chats** to resume or rename one, and use **Load earlier messages** for older history. Long conversations automatically summarize older active context while retaining the saved originals. The model can search earlier chats within the current project using its memory tool.

Open **Projects → Tasks** to add tasks or change their status. These are saved work records, not scheduled background jobs.

## Files and commands

File tools operate inside the selected folder and protect `.git` internals. Text-file reads and writes are limited to 2 MiB; links and non-text files are rejected. Files such as `.env` inside the project remain accessible.

To review without file edits, disable **Settings → General → Allow edits in the selected project**. File changes provide diffs and backup IDs. Ask the agent to restore a specific backup ID when needed. Restoring a creation removes that file; restoring a move recreates its source while leaving the destination copy.

Commands require individual approval and run with the host account's normal permissions. They are not confined to the project directory and may access the network or other files. Each command runs for at most 60 seconds.

## Research and screen context

Enable the globe to allow web research. Search queries go to public services, and returned snippets include source URLs. This does not enable interactive browser control.

In the Windows app, the screen button captures a preview of the primary display for a vision-capable model. Capture is manual; Sidekick does not continuously watch the screen or control other applications.

## Your data

Windows state is stored in `%LOCALAPPDATA%\Sidekick` unless `SIDEKICK_DATA_DIR` overrides it. This includes the SQLite database, attachments, backups, logs and WebView state. Close Sidekick before copying the entire directory for a consistent backup. Data is not encrypted by the app and should not be committed to Git.

If Ollama is offline or the model list is empty, start Ollama and choose **Settings → Models → Refresh models**. Initial model loading can take time. If a model reasons without producing a final answer, disable reasoning or increase response length.
