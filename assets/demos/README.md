# Forge demo media

The 5.1.8 captures use disposable synthetic conversations and model reasoning.
They show an earlier reading position retained after completion, with Jump to
latest still available. The light image uses a narrow layout and enlarged text.
No model inference, external requests or personal workspace data are used.
Both PNG files contain no embedded text metadata and were visually reviewed.

| File | Demonstration |
| --- | --- |
| `forge-5.1.8-reasoning-dark.png` | Saved reasoning retains the reader's position in the dark workspace. |
| `forge-5.1.8-reasoning-light.png` | The same reading controls on a narrow, enlarged light workspace. |

Private production checks cover delayed history loading, owned focus, deliberate
focus cancellation, drafts, keyboard controls and reduced motion. Browser profiles,
application state and raw logs remain outside this folder.

## Previous 5.1.6 captures

The 5.1.6 connection-editor captures use disposable synthetic data. The dark and
light desktop images show the primary setup controls and compact **Advanced**
disclosure. Connection testing is simulated; no model inference or external
requests run. Both PNG files contain no embedded text metadata.

| File | Demonstration |
| --- | --- |
| `forge-5.1.6-engine-dark.png` | Connection setup using Forge's existing dark palette. |
| `forge-5.1.6-engine-light.png` | The same editor in the light workspace. |

Production-frontend captures were visually reviewed. Private keyboard, narrow
layout, enlarged-text, contrast and reduced-motion checks also passed. Their
logs, browser profile and application state are not included.

## Previous 5.1.4 captures

The 5.1.4 screenshots use the synthetic **Tide Studio** fixture in a disposable
workspace. They contain no personal conversations, project folders, account
details or API keys. The connection test is simulated; no model inference runs.
PNG files contain no embedded text metadata.

| File | Demonstration |
| --- | --- |
| `forge-5.1.4-goal-mode.png` | A Goal mode selected with Enter and an ordinary editable prompt. |
| `forge-5.1.4-context.png` | Context capacity colors and reasoning information. |
| `forge-5.1.4-engine-presets.png` | Editable LM Studio setup and explicitly advertised model facts. |
| `forge-5.1.4-helper-summary.png` | Retained helper findings after its temporary conversation was removed. |

These images were captured from the production frontend and visually reviewed.
Private keyboard, narrow light layout, enlarged-text and reduced-motion checks
also passed. Their logs, browser profile and application state are not included.

## Previous 5.1.1 captures


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
