# Forge 5.2.1

This preview retains the native model integration described in the
[model guide](FORGE_520.md) and corrects its release packaging and preview readiness.

PyInstaller discovered a second copy of a CUDA dependency already present in the
pinned shared runtime. Builds now remove only byte-identical, verified aliases
from a fresh build directory before signing. The shared runtime and its integrity
checks remain intact. CPU dictation availability is checked in the frozen model
smoke test. Both release artifacts must fit the existing authenticated size limit
before release metadata is written.

Explicit preview opening waits for its current navigation before inspection.
Readiness is bounded and checks cancellation and preview ownership; it does not
reopen a browser or replay a tool action. The native lifecycle check exercises
this same opening path before checking stable layout and browser operations.

An installed preview can keep running while a new release is built and published.
Installing a downloaded update still waits for saved state and finished work.
Model files and local settings are independent of release packages.

## Optional uncensored Coder variant

Model setup can explicitly select **Strata Coder Uncensored**. This community
variant uses the same pinned Coder weights and a small, separately verified
[runtime projection](https://huggingface.co/alesha-pro/Qwen3.8-Flash-Next-abliterated-GSQ-RCO-Coder-Strata-GGUF).
Its fixed Strata recipe is enabled only for that model. General GGUF imports and
the original Coder keep their existing behavior. A compatible installed weight
or draft file is reused by a verified hard link where available; selecting the
variant alone starts no download.

The projection and draft head use Qwen Community License 1.0. The existing license
notice remains visible before download. Community behavior and quality claims
are not guarantees; Forge validates file compatibility, synthetic text and tool
continuation locally.
