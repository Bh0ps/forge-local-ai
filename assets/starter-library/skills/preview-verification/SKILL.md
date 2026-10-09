---
name: preview-verification
description: Exercise acceptance flows in managed previews and gather browser, console and visual evidence.
license: MIT
---

# Verify the live app

## When to use

Exercise acceptance flows in managed previews and gather browser, console and visual evidence.
Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the acceptance criteria and confirm the preview belongs to the current project/worktree. Wait for verified readiness rather than trusting server output.
2. Inspect a fresh snapshot, choose returned targets and exercise representative flows with disposable inputs. Inspect again after every navigation or interaction.
3. Check runtime/network errors, missing assets, overflow and loading/error states. Capture desktop and mobile evidence linked to the current revision.
4. Use capable image review for appearance claims. Report unmet criteria and exact evidence; never claim a screenshot or a passing build proves every flow works.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
