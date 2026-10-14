# Forge 5.1.18

## Current model details after connection changes

The public model-details action now uses the same guarded provider pool as model
catalog loading. A response from an old connection cannot publish its loaded
context after the connection is edited or invalidated while the read is running.
Forge checks the current provider object, configuration and metadata generation
after the read, and gives a bounded recovery message when the owner has changed.

Existing metadata sharing is preserved. Concurrent readers share a stable read;
an already waiting reader can refresh the current connection after invalidation.
A single stale owner refuses without starting another read. These checks concern
model metadata and do not repeat inference or completed tool actions.

Ollama's model-details shape, LM Studio's bounded unknown-model fallback, exact
loaded-instance matching and Docker Model Runner's specific recovery remain
unchanged. Pre-request refusals and catalog HTTP authentication failures retain
their existing categories.

## Bounded metadata errors

A compatible-provider catalog can fail while constructing its HTTP client, before
the request begins. Operating-system errors on that path now receive fixed
metadata recovery text instead of reflecting raw exception details. Failures
looking up the current provider after a completed read use a fixed configuration
change message; they do not claim that no request was sent.

## Validation

The affected backend gate passed 268 checks, including 26 new regressions. Tests
use fake transports, disposable state and memory-only credentials. They cover
connection edits during reads, invalidation, waiting readers, bounded errors and
preserved provider contracts. This is synthetic regression evidence, not a claim
of a reproduced failure with an installed provider.

The frontend code is unchanged. TypeScript, the production build and 16 current
production contexts passed. The prior frontend suite result is retained with an
explicit source-equivalence check; the current full Windows CI suite and sealed
package checks gate publication. No model benchmark is claimed.
