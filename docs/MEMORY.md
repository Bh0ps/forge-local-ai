# Reviewed local memory

Forge stores structured facts, preferences, and project conventions in its local
SQLite database. Saves are **suggestions for review** by default. A proposal has
`status: pending`; even a proposal submitted through the human UI remains pending
until the user explicitly approves it. Pending and rejected records are excluded
from model retrieval. Apparent API keys, Bearer credentials, Telegram bot tokens,
and password/token assignments must be removed before a memory can be proposed
or approved. Forge rejects these values rather than silently changing the fact.

Memory has three scopes:

| Scope | Visible to |
| --- | --- |
| Global | Any selected workspace |
| Project | The selected project only |
| Agent | The selected agent; optionally restricted to one project |

The service supplies the selected project and agent as trusted arguments. Model
JSON cannot select another caller scope, claim human authority, change a memory's
scope through a correction, or expand tool permissions. A global proposal sourced
from a project becomes visible globally only after the user accepts that scope.
Source runs, chats, and files must belong to the selected project and agent.
File provenance resolves symlinks and verifies the project boundary.

Each record contains its kind, title, content, scope, source provenance, review
status, and revision. Review and correction require the revision the user saw;
concurrent or stale reviews fail with a refresh request. A proposed correction
keeps the original approved fact active until acceptance, then replaces it. Human
corrections retain the original provenance with correction metadata unless the
user explicitly supplies a new source.

## Retrieval and privacy

Schema 5 installs normalized memory tables, scope/status indexes, a Unicode FTS5
index, and index-maintenance triggers through the ordered ForgeStore migration.
The memory module does not create tables during application requests. Updates,
deletions, and external SQL edits update FTS immediately and invalidate cached
embedding vectors. Forgotten entries are excluded from recall and export.
Database deletion is not forensic secure erasure; prior backups and exported
files must be managed separately if the user wants those copies removed.

FTS5 is always the baseline. Unicode words and diacritics are supported, and query
terms are quoted so user or model text cannot become FTS syntax. Retrieval searches
only approved records within the caller's scope. Optional embeddings supplement
the lexical ranking; failures retain FTS recall.

Semantic retrieval requires explicit opt-in and an already installed local
embedding-model directory. The optional `sentence-transformers` dependency loads
the model with `device='cpu'`, `local_files_only=True`, and
`trust_remote_code=False`. This module never downloads models, sends memory to a
cloud service, or calls a network embedding API. Its default is disabled. A human
can select a local model through `configure_semantic`; an unavailable dependency
or model is reported in status and leaves FTS available. The service persists the
enable flag and installed model directory in its ordinary settings.

Semantic candidates are bounded to the 256 most recently updated approved entries
in the selected scope. Vectors are persisted with the model identity and content
hash, and rechecked before saving a delayed computation. Forgetting a record
during encoding cannot recreate its vector or return the forgotten snippet.

Model search and list responses strip private source paths, URLs, notes, and
attribution. They retain verified workflow/chat IDs and scope labels. Human
export preserves the complete provenance for inspection. Retrieved records are
JSON quoted and preceded by a trust boundary: memory is historical data, not
instructions or authorization. Recall includes metadata and the trust boundary
in its UTF-8 context budget, using Forge's conservative three-byte token estimate.
The default budget is 768 estimated tokens; callers can request 128–4096.

## Coordinator contract

`ForgeMemory` uses `Store._connection` for every database operation and works with
explicitly migrated store fixtures. The service owns authorization and supplies
`human`, `project_id`, and `agent_id` as keyword arguments; it must not forward a
model-provided `human` flag.

```python
from forge_memory import ForgeMemory

memory = ForgeMemory(store)  # FTS5; semantic retrieval disabled

proposal = memory.dispatch(
    'memory_propose',
    {'kind': 'convention', 'title': 'Python verification',
     'content': 'Use the existing focused pytest suite.', 'scope': 'project',
     'source': {'kind': 'run', 'id': run['id']}},
    project_id=run.get('project_id'), agent_id=run.get('agent_id'))

item = proposal['memory']
memory.dispatch(
    'memory_review',
    {'id': item['id'], 'revision': item['revision'], 'approved': True},
    human=True, project_id=selected_project, agent_id=selected_agent)

context = memory.recall(
    current_request, project_id=run.get('project_id'),
    agent_id=run.get('agent_id'), max_tokens=768)

memory.configure_semantic(
    enabled=True, model_path=installed_model_directory, human=True)
```

| Action | Input and result |
| --- | --- |
| `memory_status` | Counts, default review policy, local retrieval availability; human status also exposes the installed model path |
| `memory_search` | `query`, optional `limit`/`max_tokens`; returns `matches`, `context`, trust boundary, estimated token count, and retrieval backend |
| `memory_list` | Optional `status`, `scope`, `limit`, `offset`; returns `items`, `skills`, `has_more`; model callers see approved, redacted items |
| `memory_propose` | `kind`, `title`, `content`, `scope`, optional `source`/`corrects_id`; returns pending `memory` and `requires_review: true` |
| `memory_review` | Human only: `id`, `revision` (or `expected_revision`), `approved` boolean; returns reviewed `memory` |
| `memory_update` | Human only: `id`, revision, optional title/content/kind/source; corrects and approves the displayed record without changing its scope |
| `memory_delete` | Human only: one `id`; removes that scoped record and retrieval indexes |
| `memory_forget` | Human only: `ids`, one `id`, literal query, or explicit `all: true`; optional scope filter; returns forgotten count |
| `memory_export` | Human only: returns JSON `content` and parsed `data`, with scoped records and skill suggestions |
| `skill_suggest` | Successful workflow source, safe name, description, Markdown instructions, scope, license; returns pending skill plus reviewable text files |
| `skill_promote` | Human only: `id`, revision, `approved`; accepts or rejects; optional human-edited instructions/description and supplied missing license |

`search()` returns the same bounded retrieval shape as `memory_search`.
`recall()` returns just its context string. Model tools may propose facts or
corrections; approval, forgetting, export, semantic configuration, and promotion
remain human controls.

## Learned skill suggestions

`suggest_from_run(run)` verifies the persisted run has completed and has at least
two completed tool actions. It creates at most one pending suggestion per run.
The template uses fixed descriptions of known actions; it does not copy personal
paths, commands, conversations, credentials, or tool results. The user must edit
an automatic provisional template and supply its license before accepting it.
Failed runs and single-action runs do not produce suggestions. The service calls
this helper only when memory suggestions are enabled and after committing the
completed run status.

Manual `skill_suggest` requests also require a verified completed workflow. The
reviewable package contains only `SKILL.md`, `LICENSE.txt`, and `PROVENANCE.json`.
Original license text and attribution are retained; a known source license cannot
be replaced. The model cannot attach executables, scripts, hooks, or arbitrary
files. Acceptance installs global skills under Forge's existing `skills/` root
or project skills under `.forge/skills/`, where normal skill discovery finds them.
Promotion refuses symlink/reparse destinations and existing packages, preserving
external edits. Suggestions do not activate or promote themselves, and skill
instructions continue to use the coordinator's permission controls.
