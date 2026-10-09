---
name: skill-authoring
description: Make compact reusable workflows with explicit routing, safe resources and evaluation cases.
license: MIT
---

# Create and evaluate skills

## When to use

Make compact reusable workflows with explicit routing, safe resources and evaluation cases.
Use during plan, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the repeated task and existing skills. Define when the workflow applies, exclusions, prerequisites and observable success.
2. Write concise portable SKILL.md guidance with valid tool examples. Keep longer guides/templates in relative resources and preserve provenance/licenses.
3. Use optional forge-skill.json for phases, intents and dependencies. Metadata cannot grant tools or permissions.
4. Preview routing against positive, paraphrase and negative examples. Review edits and version history before enabling; never promote unreviewed learned instructions.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
