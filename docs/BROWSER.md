# Browsing alongside your agent

The **Browser** tab in Forge's right panel uses the Windows WebView2 runtime.
It is the same page used by native `browser_navigate`, `browser_inspect`,
`browser_click`, `browser_type` and screenshot tools. A frontend toolbar owns
the URL, back, forward and reload controls. Opening a page requires no first-use
Chromium download. Standalone browser/Docker clients retain web research and
optional isolated Playwright tools.

The native browser is a child control inside the Forge window, with its own
profile under `.forge/state/native-browser-profile`. Remote pages receive no
Forge JavaScript/Python bridge, host objects or web messaging. Downloads and
camera/microphone/site permissions are disabled. Login, file inputs and password
entry require direct user interaction rather than model tools. External pages
cannot navigate the authenticated Forge application surface.

Forge independently inspects its own internal placeholder before positioning
the child control; frontend coordinates alone are not trusted. Bounds use the
actual owner viewport and DPI/zoom. Hidden panels, HUD mode, dialogs, stale
rectangles and positions outside the viewport are rejected. Switching tabs or
entering HUD hides the browser while keeping its document and history. Quit
disposes it. Owner resize hides the overlay immediately until fresh bounds are
validated.

Browser tools follow the permission profile. Each inspection supplies a snapshot
with stable target selectors, signatures, run identity, URL, generation and
expiry. Actions validate the DOM immediately before dispatch. Changed targets
are a known rejection; a timeout after a dispatched click remains an unknown
outcome that needs inspection, so the action is not silently repeated. Stop
invalidates snapshots and cancels pending work.

## Connect an existing Chrome or Edge tab

1. Open Settings → Browser and enable the extension bridge.
2. Open the bundled extension folder, then use **Load unpacked** in the browser's
   Extensions page with Developer mode enabled. The source folder is
   `browser-extension`; the Windows package places it in `_internal`.
3. Copy the displayed extension ID into Forge's **Register native host** form.
   Forge finds its bundled messaging executable automatically. Chrome and Edge
   may need separate IDs; registration retains the explicitly allowed IDs.
4. Click the orange Forge toolbar icon on the tab you want to connect. **ON**
   marks a selected tab. Click again to disconnect. A cross-origin navigation
   drops access and requires another explicit click. **!** indicates a bridge
   disconnection; reopen Forge and click to reconnect.

The extension uses `activeTab`, scripting and native messaging; it does not ask
for unrestricted host access or read other browser profiles. The native host
checks its registered extension origin. Authenticated loopback IPC gives each
host session a unique opaque tab ID, so identical numeric tab IDs in Chrome and
Edge cannot target the wrong browser. Only the selected tab appears in
`browser_tabs`. Tool snapshots are bound to the run that inspected them.

After Stop, an unsent action is safely cancelled. An already dispatched action
needs inspection. A disconnected tab never receives automatic retry. Very large
extension screenshots may exceed native-message limits; use the built-in Browser
panel for those captures. Forge does not distribute a Chrome Web Store listing
or bypass the browser's extension installation confirmation.

Optional isolated Playwright browsing remains available in Browser settings.
Install its separate Chromium runtime only if you choose that backend. Engine
diagnostics run only when expanded; native readiness does not start Node or
download a browser. Existing browser credentials are never copied into the
isolated or native Forge profile.
