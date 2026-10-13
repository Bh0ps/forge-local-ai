# Forge 5.1.1 demo media

These screenshots and animations use the synthetic **Tide Studio** fixture in a
disposable workspace. The chat, project, notes and sample application are demo
content. No personal account, conversation, project folder or API key is shown.

| File | Demonstration |
| --- | --- |
| `forge-5.1.1-workspace-dark.png` | Chat, saved task activity, skills and reference notes in the dark theme. |
| `forge-5.1.1-settings.png` | Grouped settings navigation, appearance and conversation controls. |
| `forge-5.1.1-activity.png` | Current-chat activity with an expanded saved tool step. |
| `forge-5.1.1-builder-light.png` | Builder and its isolated local application preview in the light theme. |
| `forge-5.1.1-context.gif` | Selecting skills and notes, writing a draft and returning to chat. |
| `forge-5.1.1-builder.gif` | Reviewing a brief, starting a preview, trying the sample app and inspecting demonstration checks. |

The previous `forge-5.1.0-*` captures remain available for that release.

Each animation contains seven captured UI states. Screenshots have no embedded
text metadata; animations contain only standard playback metadata. All frames
were reviewed before publication. The loopback URL visible in Builder belongs
to the disposable local preview.

To regenerate after a frontend build, use the development environment:

```powershell
& ./.venv/Scripts/python.exe scripts/capture_demo_media.py --output assets/demos
```

The capture script creates fresh application state and a fresh browser profile,
blocks requests outside its loopback servers, and performs no model inference.
Keep generated validation logs and application state outside this media folder.
