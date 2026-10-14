# Forge 5.1.11 performance and evidence refinement

This preview reduces repeated Performance metadata reads, adds a Textgen engine
connection and makes saved desktop evidence easier to read. It preserves Forge's
colors, chat modes, permissions and existing project workflows.

## Selected-model performance

One Performance request now shares its authoritative model, capability and
engine-version read across profile validation and tuning availability. A stale
profile or failed metadata read cannot cause the same requests to repeat for
every remaining profile. The next request retries unavailable metadata.

The outcome is private to that operation. Applying or approving a profile and
admitting another run still obtain fresh facts. Unrelated or invalid profiles
are checked before requesting metadata; provider changes prevent a previously
valid candidate from being applied. Model, context, reasoning, token and
temperature choices remain preserved by automatic profile use.

A disposable fixture with ten stale approved profiles previously made 33
model/capability/version requests per warm Performance refresh and 30 during
automatic admission. Both paths now make three requests. Stale profiles remain
unusable. These are software request counts, not measured model throughput.

Unknown context limits no longer become guessed 32K recommendations. Missing,
failed or invalid metadata leaves manual settings intact. An approved profile
that fails its current checks is labeled as needing a recheck.

Incomplete legacy display records stay readable. Malformed saved timings cannot
be approved, applied or outrank a valid profile; each measurement must have a
finite, positive numeric duration. This closes a corrupt-record robustness gap,
not a demonstrated failure of the normal calibration factory.

## Textgen connection

Textgen, formerly Oobabooga's text generation web UI, joins the existing engine
suggestions. Its editable default is `http://127.0.0.1:5000/v1`. Start its API
server separately and load the desired model in Textgen; Forge does not install
an engine, download weights or load a model when selecting or saving a preset.
An optional configured API key uses the existing private credential vault.

Textgen's catalog describes its currently loaded model. Its generation endpoint
does not choose a model from the request, so Forge verifies the exact loaded
identifier before each admitted generation and checks the model on every normal
streamed packet before exposing text, reasoning or tool calls. A missing or
changed identifier stops continuation with a fixed failure. Explicit provider
errors retain their existing authentication and quota recovery information.

The preset grants no tools, reasoning controls or context capacity. Those facts
must come from the existing explicit configuration and supported engine
contracts. It does not change generic compatible, LM Studio or OpenRouter
behavior. See the official [Textgen API guide](https://github.com/oobabooga/textgen/wiki/12-%E2%80%90-OpenAI-API).

## Saved desktop evidence

Enlarge an already recorded snapshot to fit the available space or inspect its
original size. Keyboard controls and Escape keep this view usable in narrow and
enlarged-text layouts. Opening it reuses saved pixels; it performs no new capture
or native action. Changing the conversation, request, project, transport or
snapshot closes the enlarged view.

The Browser/Desktop preference follows the existing stable desktop identity.
It is stored separately from legacy pane widths and tabs, so rolling back does
not discard those settings. Restoring Desktop does not start the native browser;
switching from an active browser still waits for its final Hide acknowledgement.

Desktop evidence remains an earlier snapshot. Shared Windows actions can use
your pointer and activate applications; this preview adds no isolated desktop
or second OS cursor. [The computer-tool guide](FORGE_5110.md) explains those
boundaries and the original implementation.

## Native verification

The standalone preview smoke fixture observes matching DOM and native bounds
before its first viewport check. Mounting can still adjust the pane after it
becomes visible. Readiness waits within the existing deadline, and the final
runtime layout guard remains active. Separate layout/viewport phase labels
improve failure diagnostics; dispatched actions are never repeated.
