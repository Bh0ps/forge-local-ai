# Forge 5.1.10 computer activity preview

This preview develops Forge's original Windows computer tools and makes their
activity easier to follow inside the existing Browser pane. It preserves the
workspace's colors, layout, permission controls and durable run history.

## Desktop evidence

The Browser/Desktop selector opens a read-only view of the current chat's
completed computer activity. Opening it does not inspect Windows, capture new
pixels, start an agent or grant permission. It shows the latest acknowledged
inspection and marks saved pixels as an earlier snapshot. Input values,
clipboard contents, raw errors, accessibility targets and app executable paths
are excluded from the compact overview.

The native browser is hidden before the Desktop view appears. Switching chats
or projects invalidates pending reads. Stop all agents remains available during
update preparation; its acknowledgement means the coordinator requested a
stop, not that every external operation has already finished.

## Supported Windows controls

`computer_inspect` supports accessibility-only inspection with
`include_image:false`. Coordinate input requires a usable fresh image, as well
as the existing same-run target, foreground and permission checks.

New image inspections request the target application's window rendering through
a bounded private helper. They do not fall back to copying desktop pixels.
Unsupported or blank rendering and timeouts leave accessibility evidence
available without an image. PrintWindow is application-dependent; modern GPU
surfaces may not render through this API. Older saved screenshots can include
overlapping windows and retain their earlier-snapshot warning.

`computer_set_value` uses a writable UI Automation ValuePattern for a literal
edit. Forge does not issue pointer, keyboard or focus input for this action.
The target application's accessibility provider may nevertheless activate its
window. A disposable native control test observed that behavior; this feature
does not guarantee that your foreground window stays unchanged.

Read-only, password, unsupported, changed, expired or cancelled targets are
refused. Attempted value changes consume their snapshot even when the outcome
is uncertain. There is no automatic fallback to typing, clipboard access,
coordinate input or repeated actions.

The queued operation retains the exact admitted arguments and app target.
Permission, session and cancellation checks are repeated at native admission
and before returning evidence. Unexpected native failures have fixed messages;
errors after a dispatched effect remain uncertain and cannot be replayed.

Shared-desktop clicks and keys still use your Windows focus and pointer. This
preview does not provide an isolated Windows desktop or a second OS cursor.

## Agent guidance and image transport

The reviewed Windows-controls workflow teaches agents to resolve exact app
identities, inspect fresh targets, distinguish supported literal edits from
shared-desktop input, verify effects and preserve uncertain actions. Its full
primary instructions fit the existing 8K context allowance alongside compact
supporting-workflow references. Existing edited, deleted, disabled and manual
skill choices remain preserved when the library upgrades.

Vision requests identify image formats from verified bytes instead of labeling
every attachment as PNG. Original encoded pixels are preserved. Invalid,
oversized or unsupported image payloads fail with content-free errors.

## Implementation boundaries

Forge's implementation is original and uses public Windows APIs. Local package
inspection informed the interface review; proprietary application code,
binaries, credentials and assets are not included in Forge.

The direct value API is documented by Microsoft:
[IUIAutomationValuePattern](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationvaluepattern).
Window rendering uses [PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow).

Native computer tools stay behind the existing desktop setting and
coordinator-owned permission registry. Tool availability or skill guidance
does not authorize broader access. Screenshots can contain private application
content; release images and validation fixtures use synthetic data.
