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

LIBRARY_VERSION = "1.0.0"
ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "starter-library"
STARTER_SKILLS = (
    {"id": "project-discovery", "name": "Explore a project", "description": "Find the entry points, project instructions and architecture before making changes.", "category": "Coding", "tags": ["architecture", "exploration", "onboarding"], "triggers": ["explore", "architecture", "codebase", "entry point", "understand this project", "project structure"]},
    {"id": "plan-and-build", "name": "Plan & build", "description": "Turn a request into a concrete implementation plan, then follow an approved build.", "category": "Planning", "tags": ["planning", "implementation", "features"], "triggers": ["plan", "build", "implement", "feature", "create", "develop"]},
    {"id": "debug-errors", "name": "Debug a problem", "description": "Reproduce failures, trace their cause and verify the smallest reliable fix.", "category": "Coding", "tags": ["debugging", "errors", "reliability"], "triggers": ["debug", "bug", "error", "crash", "failure", "broken", "not working", "doesn't work", "does not work", "fix"]},
    {"id": "code-review", "name": "Review code", "description": "Check behavior, security and regressions with evidence and precise file references.", "category": "Coding", "tags": ["review", "security", "quality"], "triggers": ["review", "audit", "security", "regression", "vulnerability", "check this code"]},
    {"id": "testing", "name": "Test & verify", "description": "Choose meaningful checks for the change and explain what the results prove.", "category": "Coding", "tags": ["testing", "validation", "quality"], "triggers": ["test", "tests", "verify", "validation", "coverage", "pytest", "vitest"]},
    {"id": "research", "name": "Research with sources", "description": "Compare primary sources, preserve citations and separate facts from uncertainty.", "category": "Research", "tags": ["research", "citations", "sources"], "triggers": ["research", "look up", "search", "latest", "compare", "sources", "citation"]},
    {"id": "browser-research", "name": "Use the browser", "description": "Read and interact with connected pages using fresh snapshots and explicit targets.", "category": "Research", "tags": ["browser", "web", "computer use"], "triggers": ["browser", "webpage", "website", "web page", "connected tab", "navigate", "screenshot"]},
    {"id": "git-workflow", "name": "Git & worktrees", "description": "Inspect changes, preserve existing work and prepare reviewable Git operations.", "category": "Workflow", "tags": ["git", "worktrees", "pull requests"], "triggers": ["git", "commit", "branch", "worktree", "pull request", "merge", "github"]},
    {"id": "documentation", "name": "Write documentation", "description": "Keep setup, usage and technical notes accurate, concise and easy to follow.", "category": "Writing", "tags": ["documentation", "readme", "writing"], "triggers": ["documentation", "document", "readme", "guide", "release notes", "changelog", "explain"]},
    {"id": "long-task-checkpoints", "name": "Stay on track", "description": "Use durable goal checklists and evidence to continue long tasks without repeating work.", "category": "Planning", "tags": ["goals", "checklists", "continuity"], "triggers": ["goal", "todo", "to-do", "checklist", "long task", "continue", "resume", "step by step"]},
)
STARTER_BY_ID = {item["id"]: item for item in STARTER_SKILLS}
STARTER_BUNDLES = (
    {"id": "research", "name": "Research essentials", "description": "A portable research workflow with source checking and browser guidance.", "category": "Research", "tags": ["research", "browser", "citations"], "skills": ["research", "browser-research"]},
    {"id": "review", "name": "Code quality", "description": "A portable review workflow with debugging and meaningful verification.", "category": "Coding", "tags": ["review", "debugging", "testing"], "skills": ["code-review", "debug-errors", "testing"]},
)

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
        except (OSError, ValueError) as exc:
            errors.append({"source_id": "forge-starter", "skill_id": identity,
                           "error": "Starter skill could not be prepared (" + type(exc).__name__ + ")."})
    if state.get("version") != LIBRARY_VERSION:
        state["version"] = LIBRARY_VERSION
        changed = True
    return changed, errors


def starter_metadata(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    for item in STARTER_SKILLS:
        if path == root / "skills" / "forge-starter" / item["id"] / "SKILL.md":
            expected = ASSET_ROOT / "skills" / item["id"] / "SKILL.md"
            return {**item, "library_id": item["id"], "builtin": True, "source": "builtin:skill/" + item["id"],
                    "license": "MIT", "version": LIBRARY_VERSION,
                    "modified": not expected.is_file() or file_digest(path) != file_digest(expected)}
    return None


def relevance_score(skill, query):
    query = str(query or "").casefold()[:24000]
    if not query.strip():
        return 0
    terms = skill.get("triggers") or skill.get("tags") or []
    if isinstance(terms, str):
        terms = [terms]
    terms = [str(term).casefold().strip() for term in terms if str(term).strip()][:50]
    # An imported automatic skill without tags can still match its readable name.
    if not terms:
        terms = [str(skill.get("name", "")).casefold()]
    score = 0
    for term in terms:
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", query):
            score += 2 if " " in term else 1
    return score


def select_skills(skills, explicit=None, query=""):
    """Explicit selection first, at most three relevant automatic skills."""
    if isinstance(explicit, str):
        explicit = [explicit]
    explicit = {str(value).casefold() for value in (explicit or [])}
    available = [skill for skill in skills if skill.get("enabled")]
    chosen = [skill for skill in available if any(str(skill.get(key, "")).casefold() in explicit
                                                 for key in ("id", "name", "library_id"))][:12]
    chosen_ids = {skill["id"] for skill in chosen}
    automatic = [(relevance_score(skill, query), skill) for skill in available
                 if skill.get("automatic") and skill["id"] not in chosen_ids]
    automatic.sort(key=lambda pair: (-pair[0], str(pair[1].get("name", ""))))
    chosen.extend(skill for score, skill in automatic[:3] if score > 0)
    return chosen[:12]
