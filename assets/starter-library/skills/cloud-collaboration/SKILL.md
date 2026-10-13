---
name: cloud-collaboration
description: Assign bounded planning, design and review work to enabled free helpers while the local agent executes.
license: MIT
---

# Use cloud specialists

## When to use

Use during plan, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Choose a listed enabled read-only helper. Supply a bounded question, original objective, scoped evidence and acceptance checks; exclude secrets/unrelated history.
2. Request observations, file/line evidence, missing checks, corrections and uncertainty. The local executor verifies and applies findings.
3. Use required for completion dependencies; advisory reviews are optional. wait=false lets independent local work continue.
4. Read agent_result with bounded waits. Inspect successful, waiting, needs_attention, unavailable and next_action; empty findings fail.
5. Only with retry_available=true, clarify the same assignment using retry_of from its failed direct child. Preserve scope/completed-tool evidence; the coordinator owns three extra attempts and model pinning.
6. Follow specific quota/authentication/consent/access/protocol recovery. Optional unavailable helpers allow local continuation; required dependencies stay blocked.

## Verification and recovery

Confirm helper claims locally with code/checks; separate suggestions from verified behavior. Never change profiles/routes, invent model aliases, replay completed actions or expose opaque provider state.

## Result packet

Outcome; findings/references; local action; unmet criteria; dependency or typed next action.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
