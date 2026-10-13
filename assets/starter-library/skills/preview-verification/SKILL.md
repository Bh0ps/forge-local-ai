---
name: preview-verification
description: Exercise acceptance flows in managed previews and gather browser, console and visual evidence.
license: MIT
---

# Verify the live app

## When to use

Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read acceptance criteria; confirm preview/project/worktree with preview_status. Server readiness does not prove correctness.
2. For visual work preview_open then preview_inspect. Use returned selectors/snapshot_id; inspect again after every interaction/navigation. Preview IDs differ from browser tab IDs.
3. Exercise disposable success, empty/invalid, loading/error/retry and keyboard flows. Observe output and persistence, not just clicks.
4. Use preview_viewport within the visible pane/native bounds. Check wide/narrow layout, long content, supported themes and enlarged text.
5. Read preview_diagnostics; separate current defects, expected negative responses and old errors. Capture final evidence; inspect images for appearance claims.
6. Map criteria to observed evidence. builder_record_check requires current revision and a completed approved session, ready preview or verified artifact. Source changes need fresh checks.

## Verification and recovery

Record criterion -> input/action -> outcome -> evidence, tested viewport/theme and unchecked branches. Refresh stale snapshots; open hidden previews. Inspect unknown dispatched outcomes before retrying; report unavailable visual review.

## Result packet

Revision/scope; acceptance outcomes/references; diagnostics; visual review status; next unresolved check.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
