---
name: project-discovery
description: Locate instructions, entry points and the data flow before changing an existing project.
license: MIT
---

# Explore a project

## When to use

Locate instructions, entry points and the data flow before changing an existing project.
Use during discover, plan. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the current request and coordinator-provided project guidance. Inspect the relevant file layout and manifests with bounded tools.
2. Trace the entry point, caller and affected data flow. Read only files needed to resolve the current question; avoid reading the entire repository.
3. Inspect existing changes before proposing edits. Record the framework, test commands and exact relevant paths for later steps.
4. Report confirmed conventions and concrete gaps. Ask only for product decisions that the repository cannot answer.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
