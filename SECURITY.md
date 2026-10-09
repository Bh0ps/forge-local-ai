# Security policy

Forge is an MIT-licensed local AI workspace. Model output, skills, webpages,
plugin manifests and MCP descriptions are untrusted input. They do not grant
permissions. First run uses **Always Ask**; approve only actions and targets you
understand. Full Access permits configured tools and should be selected knowingly.

The coordinator is authenticated and loopback-only. Native browser pages do not
receive Forge's Python bridge. Existing Chrome/Edge tabs require an explicitly
connected extension tab. Stop or disconnect a session before leaving it
unattended. Do not expose the local API directly to the internet.

Automatic Windows updates require exact repository, hash, version and trusted
timestamped publisher validation. The current unsigned preview cannot enable
automatic installation. Checksums and provenance alone are not publisher
authentication.

Report a vulnerability through the repository's
[private security advisory form](https://github.com/Bh0ps/forge-local-ai/security/advisories/new)
when available. If private reporting is unavailable, open an issue requesting a
private reporting channel without publishing exploit details or private data.
Include affected version, reproducible steps on disposable state and a redacted
diagnostic export. Do not include chats, credentials, government ID, raw logs or
personal filesystem paths. No response-time or supported-version guarantee is
claimed; fixes are maintained by the open-source contributors.
