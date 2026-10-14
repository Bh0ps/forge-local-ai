# Forge 5.1.17

## Connection-save recovery

A lost response after a committed new-engine save could previously create a second
connection when the user clicked Save again. A new editor now keeps one random
creation identity for its explicit save attempts. Existing connections retain
their identity, and changing editor owner resets the pending form state.

Save confirmation requires the returned connection identity to match. A failed or
unusable response leaves the outcome unconfirmed; Forge does not automatically
repeat a write. An explicit retry in the same editor updates the same connection
row. This is a connection-identity safeguard, not a guarantee that credential
storage writes occur only once.

After a confirmed save, the success notice appears before list/model refresh.
A subsequent model-refresh error stays visible, and provider-list errors retain
their inline recovery. The existing editor and settings layout are preserved.

## Tauri project guidance

The existing **desktop-executable** skill now covers an existing Tauri project.
Agents are guided to inspect its version, manifests, lockfiles, build hooks,
configuration and Windows overrides before selecting the actual release runner.
They retain the project's frontend and bundle settings, verify fresh embedded
assets and distinguish the app executable from its installer.

Guidance also covers observed prerequisites, notices, hashes, runtime policy and
authorized desktop verification with disposable data. SDK installation, signing,
installation and publication keep their own acceptance steps. Older Tauri projects
retain their version-matched workflow.

The library keeps 28 skill identities and the existing instruction allowance.
Detailed Tauri examples load on demand; automatic selection remains bounded and
preserves the .NET, C++ and ordinary Rust/web workflows. User-edited or deliberately
deleted installed skill content is preserved during reconciliation.

Library version: **2.4.1**. Desktop-executable recipe: **2.2.1**.

## Validation

Connection regressions use synthetic UI/API replies, disposable provider state and
fake credentials, including deliberate response loss after a real store commit.
Guidance checks validate routing, actual tool schemas, lazy resources, budget and
upgrade preservation. No live SDK download, Tauri build or model benchmark is
claimed. Full frontend, Windows CI and sealed package checks gate publication.
