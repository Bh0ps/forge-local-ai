# Skills and plugins library

Open **Library** in the sidebar. Discover searches Forge's starter collection,
selected portable packages from the official OpenAI and Anthropic catalogs,
and catalogs you connect. Search by name, description or tag; narrow the results
by type and category. Cached metadata and the shipped collection work offline.

## Ready to use

Forge includes 24 original MIT-licensed skills in six bundles. See [the Forge 5 guide](FORGE_5.md#skills-references-and-outputs) for the expanded library and workbench. The original ten remain available:

- Explore a project
- Plan & build
- Debug a problem
- Review code
- Test & verify
- Research with sources
- Use the browser
- Git & worktrees
- Write documentation
- Stay on track

The skills are enabled initially, with automatic selection for relevant tasks.
**Installed** lets you switch each skill off or disable automatic selection.
An enabled skill with automatic selection off is available for explicit selection
through `/skill` or an agent profile. Disabled skills cannot be invoked explicitly.
Activation appears in the run's activity. Selection is bounded to avoid filling
the model context with every installed skill.

Changes to these controls apply before the next model round, including a run
waiting for a question or an approval. Guidance already sent to a model cannot be
withdrawn from that in-progress request. Skills guide the agent; they do not grant
tools, credentials or permissions.

Starter files live under `.forge/skills/forge-starter`. Upgrades preserve edited
files, removed files and saved switches. Project skills can also live in
`.forge/skills`, `.agents/skills`, `.codex/skills` or `.claude/skills` inside a
connected project.

## Discover and install

Open a result to see its source, license, compatibility and setup requirements.
External entries are publisher packages and require your review. Discover only
retrieves metadata; it never installs a package or executes its scripts.
Installation stages the selected package at an immutable Git commit and preserves
license files. Confirm the staged contents before enabling it.

The supported official collection contains portable skill workflows. Host-specific
app extensions, hooks and actions require an adapter. Imported MCP connections
start disabled and need connection/authentication setup in Settings. Commands in
a skill remain subject to Forge's permission profile. Separate libraries or
accounts mentioned in a package must be installed or connected explicitly.

You can also import a local folder, ZIP or GitHub source, or connect a catalog.
Use the source controls to refresh metadata and inspect offline or error status.
The source allowlist deliberately excludes packages with redistribution
restrictions, including Anthropic's document-processing examples.

Official sources:

- [OpenAI plugin catalog](https://github.com/openai/plugins)
- [Anthropic skill examples](https://github.com/anthropics/skills)
- [Agent Skills format](https://agentskills.io/specification)
- [Claude marketplace format](https://code.claude.com/docs/en/plugin-marketplaces)
