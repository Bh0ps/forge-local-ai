"""Forge's offline starter library and small, relevance-based skill selector.

Only reviewed, bundled Markdown is reconciled automatically. Catalog discovery
fetches metadata; importing an external package still requires a user action.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile

LIBRARY_VERSION = "2.0.1"
ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "starter-library"
from forge_skill_catalog import STARTER_SKILLS, STARTER_BUNDLES
STARTER_BY_ID = {item["id"]: item for item in STARTER_SKILLS}

# Reviewed portable portions of official catalogs. These are an offline
# discovery snapshot, not redistributed third-party packages. Remote refresh
# only updates metadata and pins imports to an immutable commit.
PRESET_CATALOGS = (
    {"id": "openai-plugins", "name": "OpenAI Plugins", "repo": "openai/plugins",
     "description": "Selected portable workflows from OpenAI's public plugin catalog.",
     "catalog_path": ".agents/plugins/marketplace.json", "commit": "5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f",
     "entries": [
         {"id": "superpowers", "name": "Superpowers", "path": "plugins/superpowers", "kind": "plugin", "category": "Coding", "license": "MIT",
          "description": "Planning, debugging, test-driven development and review workflows.", "tags": ["planning", "testing", "review"],
          "setup": ["Host-specific collaboration instructions require Forge's agent adapter."]},
         {"id": "temporal", "name": "Temporal", "path": "plugins/temporal", "kind": "plugin", "category": "Coding", "license": "MIT",
          "description": "Build and debug durable applications using the Temporal SDK.", "tags": ["temporal", "workflows", "backend"],
          "setup": ["Requires a Temporal project; Temporal CLI is optional."]},
         {"id": "expo", "name": "Expo", "path": "plugins/expo", "kind": "plugin", "category": "Design", "license": "MIT",
          "description": "Create and maintain Expo and React Native applications.", "tags": ["expo", "react native", "mobile"],
          "setup": ["Requires Node.js and an Expo project.", "Codex Run actions require an adapter; skill instructions remain portable."]},
         {"id": "cloudflare", "name": "Cloudflare", "path": "plugins/cloudflare", "kind": "plugin", "category": "Coding", "license": "MIT / Apache-2.0",
          "description": "Work with Workers, Pages and Cloudflare deployment workflows.", "tags": ["cloudflare", "workers", "deployment"],
          "setup": ["Requires Node.js, Wrangler and a Cloudflare account for deployment.", "Imported MCP endpoints start disabled and require authentication."]},
     ]},
    {"id": "anthropic-skills", "name": "Anthropic Skills", "repo": "anthropics/skills",
     "description": "Apache-licensed skills selected from Anthropic's public examples.",
     "catalog_path": ".claude-plugin/marketplace.json", "commit": "683bc88e56f3e09ba94f7055977f3d3aa499f202",
     "entries": [
         {"id": "frontend-design", "name": "Frontend design", "path": "skills/frontend-design", "kind": "skill", "category": "Design", "license": "Apache-2.0",
          "description": "Design polished web interfaces with deliberate typography and layout.", "tags": ["frontend", "design", "ui"], "setup": []},
         {"id": "webapp-testing", "name": "Web app testing", "path": "skills/webapp-testing", "kind": "skill", "category": "Coding", "license": "Apache-2.0",
          "description": "Inspect and test local web applications with Playwright.", "tags": ["testing", "playwright", "browser"],
          "setup": ["Requires Python and Playwright with an installed browser."]},
         {"id": "mcp-builder", "name": "MCP builder", "path": "skills/mcp-builder", "kind": "skill", "category": "Workflow", "license": "Apache-2.0",
          "description": "Design and implement MCP servers with useful tool interfaces.", "tags": ["mcp", "tools", "integrations"],
          "setup": ["Requires Python or Node.js and the appropriate MCP SDK."]},
         {"id": "skill-creator", "name": "Skill creator", "path": "skills/skill-creator", "kind": "skill", "category": "Workflow", "license": "Apache-2.0",
          "description": "Create and refine reusable skills with supporting examples and checks.", "tags": ["skills", "evaluation", "authoring"],
          "setup": ["Supporting evaluation scripts may require Python or Node.js."]},
         {"id": "theme-factory", "name": "Theme factory", "path": "skills/theme-factory", "kind": "skill", "category": "Design", "license": "Apache-2.0",
          "description": "Apply coherent palettes and typography to presentation and web artifacts.", "tags": ["theme", "design", "typography"], "setup": []},
     ]},
)


def preset_catalog(catalog, commit=None, descriptions=None):
    commit = commit or catalog["commit"]
    entries = []
    for item in catalog["entries"]:
        entries.append({**item, "id": catalog["id"] + ":" + item["id"],
                        "source": "https://github.com/" + catalog["repo"] + "/tree/" + commit + "/" + item["path"],
                        "source_id": catalog["id"], "source_name": catalog["name"], "catalog_name": catalog["name"],
                        "description": (descriptions or {}).get(item["id"], item["description"]),
                        "version": commit[:12], "reviewed": False, "builtin": False, "featured": False,
                        "compatibility": "Portable SKILL.md; supporting tools require setup" if item["kind"] == "skill"
                        else "Portable skills and MCP; host-specific components require an adapter"})
    return {"id": catalog["id"], "name": catalog["name"], "description": catalog["description"],
            "source": "https://raw.githubusercontent.com/" + catalog["repo"] + "/" + commit + "/" + catalog["catalog_path"],
            "url": "https://github.com/" + catalog["repo"], "reviewed": False, "commit": commit, "entries": entries}


def skill_identity(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:20]


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_link(path):
    path = Path(path)
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def safe_directory(path, base):
    """Never reconcile through a redirected local package directory."""
    path, base = Path(path), Path(base).resolve()
    if not path.resolve().is_relative_to(base):
        raise ValueError("Starter library path is outside Forge storage")
    current = path
    while current != base:
        if is_link(current):
            raise ValueError("Starter library directories cannot be links")
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)


def atomic_bytes(path, content):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".forge-library-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile_starters(root, config):
    """Add new bundled skills, update only pristine files, preserve local edits.

    Deleted files remain deleted after the first reconciliation. The on/off and
    automatic preferences always use setdefault; an upgrade never resets them.
    """
    root = Path(root).resolve()
    state = config.setdefault("library", {"version": LIBRARY_VERSION, "files": {}})
    owned = state.setdefault("files", {})
    resources = state.setdefault("resources", {})
    selections = config.setdefault("skills", {})
    changed = False
    errors = []
    for item in STARTER_SKILLS:
        identity = item["id"]
        relative = "skills/forge-starter/" + identity
        folder = root / relative
        target = folder / "SKILL.md"
        try:
            safe_directory(folder, root)
            if is_link(target):
                raise ValueError("Starter skill files cannot be links")
            expected = (ASSET_ROOT / "skills" / identity / "SKILL.md").read_bytes()
            expected_digest = hashlib.sha256(expected).hexdigest()
            previous = owned.get(identity)
            if not target.exists() and previous is None:
                atomic_bytes(target, expected)
                changed = True
            elif target.is_file() and previous and file_digest(target) == previous.get("sha256") and previous.get("sha256") != expected_digest:
                atomic_bytes(target, expected)
                changed = True
            if target.is_file():
                current = file_digest(target)
                # A matching bundled file can be adopted; an existing edited file
                # records the expected digest so a later upgrade cannot overwrite it.
                record = {"path": relative + "/SKILL.md", "sha256": expected_digest}
                if previous != record and (previous is None or current == expected_digest):
                    owned[identity] = record
                    changed = True
                key = skill_identity(target)
                if key not in selections:
                    selections[key] = {"enabled": True, "automatic": True}
                    changed = True
                license_path = folder / "LICENSE"
                if not license_path.exists() and not is_link(license_path):
                    atomic_bytes(license_path, (ASSET_ROOT / "LICENSE").read_bytes())
                    changed = True
                # Upgrade owned resource files independently; preserve edits and
                # deletions exactly as for SKILL.md. Never follow package links.
                for source in (ASSET_ROOT / "skills" / identity).rglob("*"):
                    if not source.is_file() or source.name == "SKILL.md" or is_link(source):
                        continue
                    resource = source.relative_to(ASSET_ROOT / "skills" / identity)
                    destination = folder / resource
                    safe_directory(destination.parent, root)
                    if is_link(destination):
                        continue
                    resource_key = identity + "/" + resource.as_posix()
                    expected_resource = source.read_bytes()
                    resource_digest = hashlib.sha256(expected_resource).hexdigest()
                    previous_resource = resources.get(resource_key)
                    if not destination.exists() and previous_resource is None:
                        atomic_bytes(destination, expected_resource)
                        changed = True
                    elif destination.is_file() and previous_resource and file_digest(destination) == previous_resource.get("sha256") and previous_resource.get("sha256") != resource_digest:
                        atomic_bytes(destination, expected_resource)
                        changed = True
                    if previous_resource is None or destination.is_file() and file_digest(destination) == resource_digest:
                        record_resource = {"sha256": resource_digest}
                        if resources.get(resource_key) != record_resource:
                            resources[resource_key] = record_resource
                            changed = True
        except (OSError, ValueError) as exc:
            errors.append({"source_id": "forge-starter", "skill_id": identity,
                           "error": "Starter skill could not be prepared (" + type(exc).__name__ + ")."})
    if state.get("version") != LIBRARY_VERSION:
        state["version"] = LIBRARY_VERSION
        changed = True
    return changed, errors


def starter_metadata(path, root, index=None, digest=None):
    path, root = Path(path).resolve(), Path(root).resolve()
    for item in STARTER_SKILLS:
        if path == root / "skills" / "forge-starter" / item["id"] / "SKILL.md":
            expected = ASSET_ROOT / "skills" / item["id"] / "SKILL.md"
            return {**item, "library_id": item["id"], "builtin": True, "source": "builtin:skill/" + item["id"],
                    "license": "MIT", "version": LIBRARY_VERSION,
                    "modified": not expected.is_file() or ((digest or index.file_digest(path)) != index.file_digest(expected) if index else file_digest(path) != file_digest(expected))}
    return None


def _words(query):
    text = str(query or "").casefold()[:24000]
    aliases = {"verifying":"verify", "verified":"verify", "verification":"verify", "validation":"verify",
               "testing":"test", "tests":"test", "implementation":"implement", "implementing":"implement",
               "building":"build", "features":"feature", "improvements":"improve", "optimization":"optimize",
               "debugging":"debug", "instructions":"instruction", "skills":"skill"}
    return " ".join(aliases.get(word, word) for word in re.findall(r"[\w'-]+", text))


def _mentions(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)) and not bool(re.search(
        r"(?:do not|don't|without|no)\s+(?:use |run |need |want |any )?" + re.escape(term) + r"(?!\w)", text))


def task_context(query, context=None):
    context = dict(context or {})
    text = _words(query)
    groups = {
        "plan": "plan requirements specification planner",
        "build": "add build implement create develop make improve overhaul feature",
        "architecture": "architecture framework stack scaffold",
        "debug": "fix debug bug error crash broken failure regression",
        "ui": "frontend react component button website webpage dashboard form layout page mobile responsive dark mode interface",
        "design": "design visual typography palette beautiful polished vibe layout dark mode",
        "backend": "backend api endpoint server fastapi flask express service",
        "data": "database sqlite schema migration storage persist csv tsv dataset spreadsheet",
        "analysis": "analyze analysis chart dataset",
        "integration": "oauth auth authentication integration mcp credentials login sign-in",
        "test": "test verify check coverage acceptance preview",
        "preview": "preview",
        "accessibility": "accessibility accessible keyboard contrast screen reader responsive overflow",
        "performance": "performance latency inference slow responsiveness throughput speed benchmark optimize efficiency",
        "research": "research latest compare sources citation look up",
        "browser": "browser webpage connected tab navigate screenshot",
        "review": "review audit security vulnerability",
        "continue": "continue resume goal todo checklist",
        "writing": "documentation document readme guide changelog explain",
        "git": "git commit branch worktree merge github pull request",
        "delivery": "release package installer deploy distribution shipping",
        "skill": "skill reusable workflow instruction",
        "delegate": "delegate specialist helper parallel openrouter hybrid",
    }
    intents = set(context.get("intents") or [])
    for identity, terms in groups.items():
        if any(_mentions(text, term) for term in terms.split()):
            intents.add(identity)
    # Workflow phase is explicit when supplied by the coordinator. Tool results
    # can inform routing, but cannot supply instructions or permission grants.
    phase = context.get("phase")
    if phase not in {"discover", "plan", "implement", "verify", "research"}:
        phase = "plan" if "plan" in intents else "verify" if "test" in intents and "build" not in intents and "debug" not in intents else "research" if "research" in intents else "implement"
    outcomes = context.get("outcomes") or []
    if isinstance(outcomes, list):
        for outcome in outcomes[-6:]:
            if not isinstance(outcome, dict):
                continue
            if outcome.get("ok") is False or outcome.get("status") in ("failed", "error"):
                intents.add("debug")
            if outcome.get("name") in ("write_file", "edit_file") and outcome.get("ok") is not False and phase == "verify":
                intents.add("test")
    project_type = context.get("project_type", "")
    if intents & {"build", "debug", "test", "design", "preview"}:
        if project_type in ("frontend", "web", "react", "vite"):
            intents.add("ui")
        elif project_type in ("python", "backend"):
            intents.add("backend")
    return {**context, "phase": phase, "intents": intents, "_routing_resolved": True, "_query_words": text}


def relevance_score(skill, query, context=None):
    text = context["_query_words"] if context and context.get("_routing_resolved") else _words(query)
    terms = skill.get("triggers") or skill.get("tags") or [skill.get("name", "")]
    terms = [terms] if isinstance(terms, str) else terms
    score = 0
    for term in terms[:50]:
        term = _words(term)
        if term and _mentions(text, term):
            score += 3 if " " in term else 2
    for excluded in skill.get("exclude_triggers", []):
        if _words(excluded) in text:
            return 0
    routing = context if context and context.get("_routing_resolved") else task_context(query, context)
    matches = set(skill.get("intents", [])) & routing["intents"]
    specialized = set(skill.get("intents", [])) - {"build", "plan", "continue", "test"}
    foundations = {"project-discovery", "plan-and-build", "long-task-checkpoints", "efficient-tools", "architecture-frameworks", "testing"}
    if specialized and not specialized & matches and skill.get("library_id", skill.get("id")) not in foundations:
        matches -= {"build", "continue", "test"}
    phase_match = routing["phase"] in skill.get("phases", [])
    if skill.get("builtin") or skill.get("intents"):
        score += len(matches) * 3
        if score and phase_match:
            score += 5
        elif score and skill.get("phases"):
            score = max(1, score - 4)
    return score


def select_skills(skills, explicit=None, query="", context=None):
    """Stable explicit choices first, then three available relevant workflows."""
    explicit = [explicit] if isinstance(explicit, str) else list(explicit or [])
    explicit = [str(value).casefold() for value in explicit]
    routing = task_context(query, context)
    tools = set(routing["available_tools"]) if "available_tools" in routing else None
    available = [skill for skill in skills if skill.get("enabled")]
    chosen, seen = [], set()
    for reference in explicit:
        for skill in available:
            if skill["id"] not in seen and reference in [str(skill.get(key, "")).casefold() for key in ("id", "name", "library_id")]:
                chosen.append(dict(skill, selection_reason="Explicitly selected", selection_score=100))
                seen.add(skill["id"])
    automatic = []
    for skill in available:
        if not skill.get("automatic") or skill["id"] in seen or skill.get("manifest_error"):
            continue
        if tools is not None and set(skill.get("requires_tools", [])) - tools:
            continue
        score = relevance_score(skill, query, routing)
        if score:
            matches = sorted(set(skill.get("intents", [])) & routing["intents"])
            reason = "Matches " + (", ".join(matches) if matches else "task wording") + " during " + routing["phase"]
            automatic.append((score, dict(skill, selection_reason=reason, selection_score=score)))
    automatic.sort(key=lambda pair: (-pair[0], str(pair[1].get("library_id") or pair[1]["id"])))
    chosen.extend(skill for _, skill in automatic[:3])
    return chosen[:12]
