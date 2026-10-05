# Plans, goals and input during a run

Use `/plan` to inspect a request and prepare a visible plan. Planning remains
read-only. Review the saved plan and choose **Build** to start its tracked goal.
Build records that implementation has started before the next model request,
keeps the original objective in the goal and creates its ordered `TODO.md`.
`/todo` and `/goal` after a reviewed plan use that same checklist. `/to-do` remains
a compatibility alias and has no separate palette entry.

The Goals panel shows active main runs, including goals waiting for an approval
or answer. Completed goals and goals without a conversation disappear there;
their checkpoints remain available in local history. Paused runs resume from
their conversation using Resume or `/resume`.

While Forge is working, type a correction and choose **Steer**. Forge saves the
message before accepting it. It interrupts an active generation, saves visible
partial output and discards interrupted tool calls. If a tool action has already
started, Forge waits for its recorded outcome before following the new direction.
Unstarted actions in that round do not run, and an unanswered permission request
is superseded. Stop remains a separate control. Steering accepts text; image
attachments stay in the draft for a subsequent request.

With a tool-capable model, add “ask me questions as you go” to request selectable
questions. The `request_user_input` tool provides two to four choices, one
recommendation and a custom answer field. Select a choice or type your answer,
then explicitly Continue. A recommended option is never submitted automatically.
Questions pause inference between rounds and survive Pause and restart. They
become visible only after all tool responses in their round have been recorded,
so rapid answers cannot corrupt the tool conversation protocol.

Choices and steering are human task input, separate from permissions. They never
approve edits, commands, publishing or computer actions. The latest exact steering
message and answered question group remain available after compaction; full input
history remains in SQLite. Deleting the chat removes its question and steering
records along with the chat messages.
