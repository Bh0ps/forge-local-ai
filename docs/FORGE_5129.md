# Forge 5.1.29 preview

The first full main-branch CodeQL baseline after 5.1.28 reported nine Python
alerts. The prior PR scan reported no new findings; that did not establish a
clean full baseline. This follow-up addresses the HTTP boundary and fixture
server findings while preserving the preceding UI and recovery work.

- Pairing, action and event-stream failures return fixed recovery guidance.
  Raw exceptions, which can contain private paths or provider request details,
  are no longer included in these HTTP responses. Status codes, browser pairing,
  request limits and native desktop dispatch are retained.
- Static asset serving delegates containment and file lookup to Starlette's
  StaticFiles, with external symlinks disabled. Unknown API paths and rejected
  file paths remain 404 responses. This replaces equivalent custom containment
  checks that CodeQL did not recognize.
- The isolated contrast fixture server uses fixed MIME header values rather
  than values derived through the operating system MIME registry.
- The remaining URL alert is in a synthetic test assertion, not a URL allowlist
  or an authorization check. Its test purpose and disposition are documented
  with the GitHub alert; no production URL validation is relaxed.

Disposable regressions cover exception privacy for pairing, both action aliases,
JSON and streaming events, encoded traversal, prefix siblings, external links,
asset contents and security headers. Full CI and package results belong to the
published release, rather than being inferred from these focused tests.

Windows binaries retain the pinned Ed25519 publisher proof. They do not yet
carry an Authenticode certificate. Installation waits for unfinished local work
and preserves settings, credentials, projects, chats and model files.
