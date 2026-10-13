# Design a useful interface examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## A narrow settings page with long labels

Read current design tokens and reusable controls, then open the current preview. Inspect the form at a representative narrow width and enlarged text before choosing changes.

Call `read_file`:

```json
{
  "path": "<existing design-token stylesheet>",
  "start_line": 1,
  "end_line": 120
}
```

Call `preview_open`:

```json
{
  "id": "<current preview id>"
}
```

Call `preview_screenshot`:

```json
{
  "id": "<same current preview id>"
}
```

Call `tools_search`:

```json
{
  "query": "inspect UI contrast or image evidence",
  "limit": 5
}
```

Expected evidence: Keep the project's palette, icon family and spacing rhythm. Group related settings and place secondary explanations on demand while keeping errors/recovery visible. Inspect labels, focus, contrast and overflow in supported themes. Search only discovers an offered contrast/image tool; load its exact returned name if needed.

Recovery: Do not invent a contrast score from a screenshot or call a guessed tool. If capable image review is unavailable, report structural checks and leave visual quality unverified. Fix the smallest relevant token/component.

## One activity summary with visible recovery

The chat repeats an operational status in a message, tool card and side pane. Identify the request/run identity and choose one compact summary as the primary home; retain durable expanded steps and outputs.

Expected evidence: Verify completed and waiting states, nested helpers, reconnect/reopen and history pagination. Approvals, questions and blocked recovery remain immediately discoverable. Use concise current-step text and readable supporting labels instead of adding another status surface.

Recovery: If a simplification hides recovery or mixes unrelated runs, repair the identity/scope and information hierarchy. Avoid visually attractive placeholders that remove real operational controls.

## A quiet accent for active thinking

Add a restrained glow only around an active status. Continuous motion must stop when the app is hidden and be removed for reduced motion; streaming characters do not receive decorative typing effects.

Expected evidence: Inspect active, waiting and completed states plus hidden/show and reduced-motion changes. A reader who scrolls older thinking is not pulled down; offer a useful latest control, and initialize newly expanded live thinking at its newest text.

Recovery: Do not use animation to imply progress while waiting. If viewport, motion or scroll intent fails, remove the accent until the lifecycle behavior is correct. Preserve completed saved reasoning as readable content.
