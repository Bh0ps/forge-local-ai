---
name: git-workflow
description: Preserve existing changes and prepare isolated, reviewable Git operations.
license: MIT
---

# Git & worktrees

## When to use

Preserve existing changes and prepare isolated, reviewable Git operations.
Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect status, intended branch and relevant diff before changing Git state. Preserve dirty work.
2. Use isolated worktrees for concurrent writers. Keep changes focused and inspect conflicts rather than overwriting them.
3. Verify the final diff and appropriate checks before preparing a commit or PR. Describe the final problem, behavior and evidence.
4. Publishing, merging and destructive operations follow the user request and approval policy. Keep credentials and personal state out of Git.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
