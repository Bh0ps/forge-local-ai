# Connected workflows

Forge 4.2 supports an opt-in Telegram bot gateway and authenticated incoming and outgoing webhooks. The coordinator keeps its existing loopback binding. Telegram uses outbound long polling; enabling it does not expose an HTTP listener to the internet.

## Telegram setup

1. Create a bot with Telegram's BotFather and enter its token in Forge's Channels settings. Forge stores the token in the OS CredentialVault; saved configuration contains a credential reference.
2. Select **Connect / test** to identify the bot with `getMe`. Saving configuration alone does not connect or send a message.
3. Choose the project, installed model, optional agent profile, and maximum permission profile. Enable incoming tasks to allow Telegram requests to start work. Forge caps every run at the current local permission setting.
4. Generate a pairing code and send the displayed `/pair CODE` command to your bot. Review and approve the pending sender and chat in Forge. A valid code only creates a pending pairing request. Local approval grants that exact sender/chat combination.
5. Enable the channel. The first approved sender becomes its owner. Additional approved senders may submit tasks; the owner controls runs and approves actions.

Each approved Telegram chat maps to a stable Forge chat and its configured project. A new request waits durably when the same chat already has an active writer. Archived, moved, or removed chats/projects require local reconnection before more work can start. Researcher and reviewer profiles remain read-only; writing agent profiles use the service's managed workspace policy.

Owner commands are `/status`, `/pause`, `/resume`, `/cancel`, and `/help`. They act only on the latest run in that channel's own Forge chat. Commands cannot select an arbitrary run or another project. Resume preserves Forge's safeguards for actions whose outcomes are unknown.

Approval messages use Telegram inline buttons. A button is bound to the channel, sender, recipient chat, run, approval, action hash, and expiry, and is consumed once. Changing the action, forwarding the button to another chat, using a different sender, or replaying it cannot approve a second action. If an approval expires, use the pending approval in Forge.

## Notifications and delivery

Run approvals, terminal results, and actions with unknown outcomes appear in Forge's local notification hub. Read/unread state is saved. The outbox shows delivered, pending, retrying, failed, cancelled, and unknown deliveries. Channel polling failures also appear locally; saved incoming requests remain available after a restart.

Remote notifications include run and project metadata by default. **Include result content** explicitly allows assistant result excerpts and approval arguments to leave Forge. Credential-looking content is redacted. Result export should only be enabled for recipients authorized to see the project's contents.

Outgoing messages use a persisted outbox with bounded exponential backoff. Rate-limit responses honor Telegram's `retry_after`. Forge cannot prove whether a `sendMessage` succeeded when its response was lost: Telegram has no idempotency key for that operation. Such a delivery stops as **outcome unknown** until the user explicitly confirms a retry that may produce a duplicate notification. Retrying delivery never restarts a model run or repeats a tool action.

Run events are copied into outbox entries and local notifications in the same transaction as their channel checkpoint. A monotonic event journal prevents notification loss when old chats or events are deleted. The run-event subscriber only wakes the channel worker; network delivery cannot block inference or approval journaling.

Deleting a channel chat cancels its queued deliveries, removes its route and keeps inbound deduplication records. A later newly sent message gets a fresh chat; replaying the deleted chat's original update cannot restart its run. Removing a project registration disables its bound channels and preserves their original project identity for review. Lowering a channel's permission ceiling also constrains accepted runs and their child agents.

## Incoming webhooks

Configure a webhook channel with its own CredentialVault secret, fixed project, model/profile, and enabled incoming tasks. Send JSON to:

```text
POST /api/v1/channels/CHANNEL_ID/ingest
```

The versioned ingest route verifies these headers against the original request bytes:

| Header | Value |
| --- | --- |
| `X-Forge-Timestamp` | Unix timestamp in seconds, within five minutes of Forge's clock |
| `X-Forge-Nonce` | A fresh 16–128 character URL-safe identifier |
| `X-Forge-Signature` | `sha256=` followed by a lowercase HMAC-SHA256 hex digest |

The signing input is `timestamp + "." + nonce + "." + raw_body`. UTF-8 encode the header strings, concatenate them with the body bytes, and sign with the configured secret. The HTTP body is limited to 64 KiB; request text is limited to 16,000 characters.

```json
{"id":"deploy_review_001","text":"Review the saved deployment report."}
```

Only `id`, `text`, and an optional `project_id` are accepted. The project must match the configured channel project. The sender cannot choose another model, expand permissions, select a different agent, or submit tool arguments. Nonces are rejected on replay and retained for 24 hours. A stable event `id` also deduplicates retries that use a fresh nonce. The nonce and incoming event are saved together before acknowledgement.

The HMAC exception applies only to the versioned ingest route. Ordinary Forge API routes still require their normal authenticated local client. Making an inbound webhook remotely reachable requires separate, explicit hosting configuration; Channels does not change the default network exposure.

## Outgoing completion webhooks

An outgoing webhook receives a completed-run event for its configured project. URLs require HTTPS; HTTP is allowed only for loopback fixtures. Redirects are disabled. Bodies contain event/run/chat/project/status metadata, plus a redacted result excerpt only when result export is explicitly enabled.

Deliveries include the same HMAC headers plus `X-Forge-Delivery-ID` and `Idempotency-Key`. Retries preserve this delivery ID and sign fresh timestamps/nonces. Receivers should deduplicate the delivery ID before performing side effects. Delivery retries never create another Forge request or replay a tool call.

## Service integration and fixtures

`ForgeStore` applies `CHANNEL_SCHEMA` during ordered migration 5; the channel module never creates tables lazily. `ForgeService.channel_start` commits the incoming event's run association, request identity, user message, and run atomically before inference. A repeated source key returns the original run without launching again. Interrupted runs require explicit resume rather than automatic tool replay.

`ChannelManager` exposes `start`, `tick`, `stop`, `shutdown`, `on_run_event`, and `ingest`, plus the `channel_*` configuration and notification actions. Its worker owns polling and delivery. Stopping the worker leaves pending work and checkpoints in SQLite for the next start.

Update preparation drains the channel worker under its lock before taking the state snapshot. While preparation is active, configuration changes, pairing, authenticated ingress, and delivery are rejected or paused before an acknowledgement or network send. A rollback merges the latest incoming identities, Telegram offsets, and delivery evidence into the restored database, then disables every restored channel and cancels pending deliveries. Restored bindings require a fresh credential and explicit pairing before they can run again; enabling the old configuration alone is insufficient. The updater also retains the latest database privately for inspection when the restored version predates Channels.

Tests use synthetic credentials, official Bot API-shaped HTTP envelopes, and a loopback HTTP fixture. They do not authenticate to a real bot, send a real Telegram message, or call an external completion webhook. Bot request/response fields follow the [official Telegram Bot API documentation](https://core.telegram.org/bots/api).
