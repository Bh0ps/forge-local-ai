---
name: plan-and-build
description: Turn requirements into acceptance criteria and an ordered implementation plan, then execute the started goal.
license: MIT
---

# Plan & build

## When to use

Turn requirements into acceptance criteria and an ordered implementation plan, then execute the started goal.
Use during plan, implement. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the existing implementation. Capture the user outcome, audience, affected flows and measurable acceptance criteria.
2. Use request_user_input for a missing decision that materially changes the result; offer concise options and a recommendation.
3. In plan mode, produce the concrete ordered plan without edits. Once Build or a goal starts, follow the coordinator's execution directive and first unfinished task.
4. Complete a working vertical slice, verify its acceptance criteria, record evidence and continue authorized work. Do not reinterpret a started goal as another request to plan.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
