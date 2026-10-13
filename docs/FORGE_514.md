# Forge 5.1.4 Windows preview

This preview adds composer modes, temporary helper conversations, clearer local
engine setup and measured tuning for the selected model. Forge keeps its warm
colors, project tools and saved-work checks.

## Choose a workflow in the composer

Type `/` to browse commands. Choose `/goal`, `/plan`, `/do`, `/builder` or `/todo`
with Enter or a click. The command becomes a removable **Next request** mode;
your editable prompt contains ordinary text. The selected mode belongs to that
chat's draft and is saved locally with the same revision checks as its text.

A successful workflow submission clears its selected mode. A failed submission
keeps it with the prompt. Explicit action commands retain their existing behavior
and permissions; choosing a mode alone starts no work.

## Helper results without extra conversations

Automatically created helper chats stay out of navigation. After their parent
finishes and consumes the result, Forge removes the helper's full conversation,
private provider state and working journal in a single transaction. A bounded
result, serving model, counts and assignment identity remain in parent activity.
Usage totals and duplicate-request protection remain intact.

Paused parents, unknown tool outcomes, unresolved approvals/questions, live
workers and unsent drafts retain their recovery data. User-owned or shared chats
are preserved. Cleanup runs after completion and at startup, so interrupted
maintenance can retry. SQLite reuses the freed pages; cleanup does not remove
project files, model weights or helper worktrees awaiting review.

## Local engines and model details

Connection setup includes editable presets for Ollama, LM Studio, llama.cpp,
Jan, LocalAI, vLLM, SGLang and KoboldCpp. Each provides an endpoint and official
setup link. Testing verifies the selected endpoint; a preset alone is not proof
of tool or image support.

LM Studio declarations distinguish a model's advertised maximum from each loaded
instance's context. Forge uses an exact, unambiguous loaded instance when one is
reported, within the configured engine limit. Ambiguous or missing facts stay
unknown. Advertised training and vision facts never silently enable permissions.
[LM Studio model schema](https://lmstudio.ai/docs/developer/rest/list)

The compatibility chat endpoint does not advertise a reasoning-control parameter.
Its reasoning setting therefore shows **Engine controlled**, while preserving
your saved preference. Forge does not invent a request parameter.
[Chat-completions parameters](https://lmstudio.ai/docs/developer/openai-compat/chat-completions)

The brain icon's five colors indicate configured context capacity; its label
explains the requested or reported reasoning mode. It does not claim to measure
live context occupancy. Context saves remain visible across model changes, and a
refused save offers Retry or Discard until an actual write is acknowledged.

## Selected-model performance

Eligible local Ollama models can run an explicit, bounded thread comparison.
The check preserves the selected model, context, reasoning preference and answer
controls. It warms both configurations, interleaves five measured pairs, and
checks coding, prefix, tools and advertised image behavior. A proposal requires
reported timings, a repeatable decode improvement and acceptable latency/noise.
The check stops for foreground work, changed settings, cancellation or its budget.

Approve a measured proposal before automatic use. Its authority is bound to fresh
model digest, engine, provider, hardware and selected settings. Old, changed or
incomplete measurements require recalibration. Remote engines and unknown hardware
remain advisory. Checks do not rewrite external server flags or download models.
Synthetic probe success does not guarantee every task or a completely filled
context window. No general tokens-per-second improvement is claimed.

Streaming accounting no longer keeps a second transcript buffer. Concurrent
metadata reads share one request, invalidation rejects delayed replies, and
hardware polling labels stale or unavailable samples. Partial/cancelled output
and reported usage counters retain their existing accounting behavior.
Engine failures retain fixed messages, status codes and recovery categories;
raw response bodies are excluded from saved failure records.

## Verified downloads and restart

In Updates, choose **Download verified**, **Check only** or **Off**. Verified
automatic downloads are opt-in, follow the selected GitHub channel and run when
the desktop is idle. Cancellation and bounded retry status remain visible.
Starting foreground work stops the automatic download; manual downloads remain
independent. A downloaded package still requires the saved-work and native restart
checks before installation. Automatic download does not imply unattended restart.

Publisher signatures, exact source/file binding and recovery checks retain the
[5.1.3 trust model](FORGE_513.md#downloads-trust-and-recovery). The Windows
executables do not yet have an Authenticode certificate. No database schema
migration is added; back up local state before replacing an installation.
