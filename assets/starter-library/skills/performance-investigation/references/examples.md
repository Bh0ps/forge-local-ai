# Measure and improve performance examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
run_command({"argv":["python","-m","pytest","-q","test_core_responsiveness.py"],"timeout":60})
```

Before: Define the representative task and baseline. Separate queue, model loading, prompt processing, generation, tool and UI time.

After: Compare elapsed time, prompt tokens, redundant tools, memory and task correctness. Report measured gains and unresolved limits, not just tokens per second.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
