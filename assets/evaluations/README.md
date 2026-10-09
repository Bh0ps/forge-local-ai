# Local task evaluation

The version-1 suite contains exactly twenty original tasks: twelve app/UI/coding,
four broader workflows and four continuity/recovery scenarios. Every task has
public acceptance criteria, an identical starting file snapshot and a behavioral
oracle. Reference implementations only validate the grader; live agents never
receive them.

The UI fixtures exercise forms, state, filters, monetary totals, keyboard focus,
theme persistence and accessibility/response structure in jsdom. They do not
establish visual quality in a real browser. Python fixtures test pure function
behavior and boundary cases; SQL runs in memory. Broader workflows validate
source-grounded structured data, arithmetic, dependencies and input preservation.
Continuity tests inject restart, steering, inspected unknown outcomes and forced
compaction, then check the resulting files, goal evidence and duplicate effects.

Validate all fixtures three times without model requests:

```powershell
./.venv/Scripts/python.exe scripts/evaluate_tasks.py --repetitions 3 --output ../forge-5.0-evaluation-dry.json
```

Live evaluation requires an idle local backend, stable source checkouts and a
separate output directory. It uses the exact loaded model/digest. `--load-installed`
explicitly permits warming an exact already-installed `--model`; no model is
downloaded. The benchmark's temporary 8K inference allocation is separate from
the user's persisted app context selection.

```powershell
./.venv/Scripts/python.exe scripts/evaluate_tasks.py --live --baseline ../forge-5.0-baseline --candidate . --model <exact-local-tag> --repetitions 3 --output ../forge-5.0-evaluation-matched
```

Each case uses a fresh source-import worker, isolated project/profile, temperature
0, thinking disabled, 8192 context and 2048 output limit. The task ceiling is 180
seconds, 24 model rounds and 48 tools. The same trusted `evaluation_check()` tool
is available to both versions. General commands, cloud, network research,
computer/browser automation and publishing are absent. Oracles accept no
model-supplied executable paths and run in bounded workers. These are controlled
fixtures, not an OS sandbox for arbitrary untrusted programs.

Baseline/candidate order alternates by case and repetition. Warming is recorded
separately; source hash, evaluator hash, fixture hash, exact model digest, controls,
pair order, wall time, token provenance, rounds, errors and requirement checks are
retained. Each result includes its durable checkpoint. Rerunning with the same
output resumes only matching completed measurements; source/evaluator changes
require a fresh output directory.

Read success rates before speed. The wall-time ratio includes only comparable
pairs where both versions passed the behavioral oracle. Failed, unpaired,
changed-source and synthetic fixture runs cannot demonstrate live speed gains.
Baseline-only/candidate-only invocations are diagnostics. Preliminary calibration
runs and grader corrections must be excluded from the matched suite.
