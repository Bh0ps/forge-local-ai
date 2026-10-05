# GitHub connection

Forge can connect to GitHub.com, browse repositories and organizations, bind a repository to a local project, and clone it into Forge's managed projects directory. Project tools can read selected repository files, search code, read issues, list pull requests, and propose a draft pull request. Creating a pull request always requires the user's approval for that exact repository, branches, title and body, including when the selected permission profile is Full Access. Deny Access and read-only profiles still deny publishing.

Connect from the GitHub settings page using one of these methods:

- Enter a fine-grained personal access token in the secret field. Restrict it to the repositories you intend to use. Metadata and Contents read permissions support repository browsing and file reads; Pull requests write permission supports approved draft PR creation. Organization approval or SSO policies can also apply. Some API endpoints, including code search, have separate authentication restrictions.
- Explicitly import the currently signed-in GitHub CLI account. Forge captures `gh auth token --hostname github.com` privately and moves the token into the OS credential vault. The token is never placed in command arguments or displayed.
- Choose public browsing and enter an owner or organization. No account token is used. Private repositories and PR creation require authenticated access.

Public browsing exposes only read tools. Authenticated project connections can
also expose draft PR creation, which still waits for a specific approval.

Tokens belong in the secret field, not in chat messages. Forge stores only a vault reference and minimal account/repository metadata in its local database. Disconnect deletes the credential reference and repository bindings. Reconnecting replaces the saved credential; changing accounts clears bindings. Reconnecting or reselecting a repository while an approval waits invalidates that pending action.

Only repositories explicitly selected for the current project are visible to model tools. Selecting an account does not grant every project access to every account repository. The account repository picker is a human UI action; the `github__list_repos` model tool lists the current project's saved allowlist. Repository and issue content is untrusted data and cannot grant tools permission. File reads are UTF-8, bounded to 1 MiB remotely and 48,000 characters of local output. Directory, issue, PR and search results are bounded, with explicit pages instead of unlimited pagination.

Cloning is an explicit human action into a fresh managed directory. It never overwrites an existing project, runs code, recurses into submodules, or enables repository hooks. Forge disables inherited Git credential helpers, configuration, filters and tracing for the clone, keeps the credential in a temporary process environment, and removes the fixed askpass helper afterward. Source and license files stay with the clone. Git must already be installed. Authentication does not create commits, push branches, or change remote repositories; a PR head branch must already exist on GitHub.

Forge sends GitHub REST requests only to `https://api.github.com` over HTTPS with a pinned API-version header. It does not follow redirects or download a returned content URL. A write approval is consumed once, before dispatch. If the connection breaks, a server error occurs, or the write response is incomplete, Forge marks the outcome unknown and pauses the run. Inspect the PR list on GitHub before resolving the action; Resume cannot repeat an unresolved write.

## Service contracts

`GitHubConnection(service, vault=None, client_factory=None, git_runner=None, gh_runner=None)` performs no network work at construction. Tests inject transports and in-memory vaults; there is no user-controlled API endpoint.

Human UI actions use `dispatch(action, data)`:

| Action | Fields |
| --- | --- |
| `github_status` | None; returns `connected`, `public`, `authenticated`, `login`, `account_url`, `gh_available`, `repositories`, `permissions_note` |
| `github_connect` | Exactly one of `token`, `import_gh: true`, or `public: true` |
| `github_disconnect` | None |
| `github_repos` | Optional `owner`, `organization`, `page` (1–20), `limit` (1–100) |
| `github_select_repo` | `full_name: owner/name`, optional existing `project_id` |
| `github_clone` | `full_name: owner/name`, optional `project_name`; returns `project`, `repository`, `path` |

The tool registry calls `schemas(project)` and `describe_target(name, args, run_context)`. Read schemas have capability `read`; `github__create_pull_request` has capability `write`. `execute(name, args, run_context, invocation_id=...)` takes trusted run/project authority separately from model arguments. A PR requires a matching approved record for that run and invocation, exact arguments, target and action hash. Passing `human_approved` in model JSON never grants authority. Known rejection returns `not_executed`; uncertain remote completion returns `outcome_unknown`.

```python
service.dispatch('github_connect', {'import_gh': True})
service.dispatch('github_repos', {'page': 1, 'limit': 30})
service.dispatch('github_select_repo', {
    'full_name': 'owner/repository', 'project_id': project['id']})
# The composer then exposes scoped GitHub tools. PR calls wait in the
# existing durable approval UI, which shows the exact proposed arguments.
```

There is no OAuth client registration, GitHub CLI client-ID reuse, generic model-controlled HTTP tool, issue creation, messaging, notifications subscription, or automatic publishing in this connection.

## Validation and upstream references

Offline tests cover authentication failures and redaction, reconnection, cross-project isolation, fixed-host requests, traversal and returned-URL rejection, exact approval binding and replay prevention, outcome-unknown writes, safe clone arguments, existing-directory collisions, and the real service's approval and read-only tool boundaries. They use disposable stores and simulated HTTP/Git processes.

An October 5, 2026 public smoke check browsed public repositories,
selected `Bh0ps/forge-local-ai`, read its 8,067-character README, and cloned it
into a new disposable Forge profile in about two seconds. Project registration,
README and LICENSE preservation, and rejection of another project scope passed.
All five GitHub API reads returned HTTP 200 and selected API version 2026-03-10.
The temporary profile was removed afterward. No account credential, private
repository, PR write, code execution or inference was involved.

GitHub's authoritative references: [repository REST API](https://docs.github.com/en/rest/repos/repos), [repository contents](https://docs.github.com/en/rest/repos/contents), [pull requests](https://docs.github.com/en/rest/pulls/pulls), and [fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
