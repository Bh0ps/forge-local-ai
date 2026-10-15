# Forge 5.1.28 preview

This preview combines the pending Library, connected-workflow recovery and Usage
changes with XML inspection hardening and Windows lifecycle corrections.

## Security

- Document and report XML rejects document type declarations before returning
  extracted text or accepting a generated report. Ordinary XML, namespaces,
  escaped characters and UTF-8/UTF-16 documents remain supported.
- Weekly CodeQL scans cover Python, TypeScript/JavaScript and GitHub Actions.
  Pull requests receive dependency review; Dependabot proposes reviewed updates
  for Python, frontend dependencies and pinned workflow actions.
- Workflow actions remain pinned to full commit identities. Checkout steps no
  longer retain repository credentials in their working copies.
- Repository controls are managed separately from application settings. This
  patch does not change a user's model, context, permissions or credentials.

## Windows startup and exit

- The packaged workspace check waits for page and bridge readiness before
  querying the composer, using one existing 25-second startup budget.
- Page, bridge and known SDK initialization failures receive distinct sanitized
  diagnostic categories; unknown errors remain suppressed.
- Host finalization waits for admitted tray callbacks to return after owned
  cleanup, within the original deadline. A cleanup notification alone does not
  establish that its callback has exited.
- The earlier intermittent CI crash and WebView error did not establish a single
  root cause. These changes address reproduced synchronization gaps; release
  verification still requires successful process exit and all native checks.

## Library, recovery and Usage

- Library cards and instructions have clearer hierarchy, compact filters, and
  explicit loading, empty and failure states.
- Connected-workflow recovery bounds each queue-maintenance quantum, preserves
  delivery ordering and rechecks permission/configuration before admission.
- Update checks validate derived queue structures without ignoring authoritative
  user data. Uncertain recovery pauses instead of repeating accepted work.
- Goal preparation preserves an externally created or edited goal file.
- Usage has clearer input/output legends, readable narrow layouts and honest
  loading, unavailable, empty, cached and retry states. Responses belong to the
  request and scope that initiated them.
- Existing chat, composer, palettes, drafts, dictation, HUD, Builder workflows
  and desktop permission controls are retained.

## Verification and installation

The goal-review fixture uses committed completion notifications followed by a
durable state read. Its original deadline and result/usage assertions remain.
Synthetic regressions cover XML declarations across encodings, startup readiness
and tray callback completion. Release gates include the full backend/frontend
suites, production build, packaged Windows lifecycle, installer recovery,
publisher signatures and package privacy checks. Results are recorded with the
published release after those gates complete.

The installer preserves application data. Automatic download and installation
still require the configured update policy, valid publisher proof and safe idle
state. This preview does not introduce an isolated desktop or a second OS mouse.
