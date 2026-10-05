# Start using Forge

1. Install Forge or extract the portable package and open `Forge.exe`. Start your
   existing Ollama service. Select an installed model using the composer button.
2. Open **Projects** to register a folder in place or create a managed project.
   Select the project and start a chat. Use the right panel for files and activity.
3. Start with **Always Ask**. Review proposed edits, commands and computer actions;
   **Allow once** binds to that exact action. Configure scopes under Settings.
4. Use `/plan your task` for inspection and planning, then **Build** or `/todo`
   to execute that saved plan as an ordered Markdown checklist. `/todo` followed
   by tasks creates and starts a new checklist. `/goal` creates or attaches a
   checklist and continues within its configured limits.
5. **Pause** preserves progress. **Resume** continues after a restart or limit.
   Inspect and resolve actions labeled **Outcome unknown** before resuming them.
   Externally edited TODO files show an import/overwrite reconciliation choice.
6. Switch to HUD using the header button or **Ctrl+Shift+H**. The same window stays
   on top. Expand replies on demand. Switch back to restore the workspace bounds.
7. Attach an image or desktop screenshot with a vision-capable model. In Settings,
   install a dictation model; press the microphone to start/stop dictation and edit
   the transcript before sending. Dictation uses CPU INT8.
8. Configure MCP, skills and plugins under Settings/Plugins. Inspect third-party
   packages before installation. Remote MCP supports bearer tokens and OAuth.
   Browser setup can install isolated Chromium; the optional extension connects
   selected existing Chrome/Edge tabs explicitly.
9. Configure agent profiles and schedules in their sidebar pages. Review a writing
   agent's worktree diff before integration. Scheduling inherits the permission
   policy; approvals can wait while the window is closed.
10. Close the window to leave the coordinator in the tray. Use **Quit Forge** from
    the tray to stop it. The global emergency shortcut is **Ctrl+Alt+Shift+S**.

**Ctrl+N** starts a chat. **Ctrl+K** focuses the composer. **Shift+Escape** stops the
current generation. All-time usage begins with the Forge upgrade; older chats are
retained without invented usage counts.

For runtime setup, browser extensions, Docker and plugin formats, see
[Integrations](docs/INTEGRATIONS.md). Context and performance recommendations require
an explicit selection; use a model that fits GPU memory with room for the KV cache
and image processing.
