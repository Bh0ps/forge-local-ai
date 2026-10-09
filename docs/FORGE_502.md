# Forge 5.0.2

The composer keeps its everyday controls small. The sparkle icon beside Context
(the brain) opens a docked **Context** panel: search skills and Space notes together,
select them with checkboxes, and remove attachments from the chips. Selections use
the existing restart-safe draft storage. Disabled skills cannot be activated here.

Paused chats show an icon-only **Resume** control beside the bottom-left status
indicator. Forge inspects interrupted actions before enabling it. The adjacent
recovery icon exposes details; unresolved actions still require actual inspection.
Activity retains the full run trace, review and guidance evidence.

## Develop an idea together

Use `/builder`, or `/builder a colourful habit tracker`. Forge suggests ideas and
asks at most two questions per reply, gradually developing the audience, flows,
visual direction, constraints and acceptance checks. The chat stays read-only and
remembers the guided mode across turns and restarts. Ordinary conversational
replies continue the interview. `/builder off` returns to normal chat.

Refine the proposed brief until it describes what you want. `/build` explicitly
accepts the current proposal and starts a tracked implementation in this chat's
connected project. The accepted request retains the discussion and its revisions.
A project must be connected before implementation; brainstorming works without one.
`/plan`, `/todo` and `/goal` remain available for direct requests.

## Telegram as a conversation

Enable **Conversation replies** in Channels for the paired recipients allowed to
see this chat. The switch stays opt-in for other connections. Status-only mode is
labelled clearly. Forge sends the assistant's answer, splitting longer responses
into ordered plain-text messages, rather than just announcing that a run completed.
Replies belong to their producing run. Delayed events cannot export later answers.
Responses beyond 64,000 characters are explicitly shortened; the complete original
stays in Forge. Credential-looking text is redacted before splitting.

**Connect / Test** registers the bot's native command menu. Menu failures have a
separate visible state. These commands operate only in the paired chat and its
configured project and permission ceiling:

| Command | Action |
| --- | --- |
| `/builder [idea]` | Start a guided brainstorming conversation |
| `/builder off` | Return to ordinary chat |
| `/build` | Accept the current Builder proposal in the connected project |
| `/plan request` | Inspect and prepare a plan |
| `/goal request` | Start tracked work |
| `/answer reply` | Answer the pending question; use `\|` between multiple answers |
| `/status` | Show this chat's current run |
| `/pause`, `/resume`, `/cancel` | Owner controls for this chat's work |
| `/help` | Show help |

Structured questions are sent to Telegram too. A plain-text reply answers a pending
question; use `/answer first reply | second reply` when several are waiting. Answers
do not authorize tools. Existing approval buttons remain bound to the owner, chat,
run and action. Unrecognized commands return a useful error to approved recipients.

Forge and the computer must remain running for local inference and Telegram polling.
Telegram delivery still has no idempotency key: an uncertain send stops for inspection,
and later parts wait rather than replaying or overtaking it. See
[channel setup and recovery](CHANNELS.md) and the
[Telegram Bot API](https://core.telegram.org/bots/api).

## Release status

This is an unsigned Windows preview for user testing. The release includes the
previous Forge 5.0/5.0.1 work and this minor update. The full repeated task comparison
was deferred at the user's request. Performance targets and baseline-or-better task
quality are not established by this release. Focused regression checks and a
production build are reported separately; no speed gains are claimed.
