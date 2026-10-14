# Forge 5.1.19

## Honest desktop activity labels

Desktop activity now describes the recorded operation with a compact noun:
**Target click**, **Text entry**, **Window focus** or **Field value update**.
Previously, labels such as "Clicked target" could imply success when a tool had
correctly refused the request without sending input.

The adjacent recorded-step count and request status keep their existing meaning.
A recorded step is evidence of a journaled tool result; it does not guarantee a
physical effect. Open **Activity** to inspect the actual result or refusal.
Unknown or older operation names use **Desktop operation**.

The Browser/Desktop layout, colors, saved-snapshot warning and reading controls
are preserved. This change adds no input capability or permission and does not
repeat actions. Native clicks and keys share the Windows focus and pointer.

## Validation

Coordinator controls reproduced refused click and text requests with zero native
input. Frontend checks cover operation wording, independent request status,
recorded counts, stale scope responses, retained helper activity and the keyboard
route to Activity. Production visual checks use synthetic data in the actual app.
Current full Windows CI and sealed package checks gate publication.
