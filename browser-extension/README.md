# Forge browser bridge

The extension uses `activeTab`, so it sees only a tab explicitly connected by
clicking its toolbar button. Click again to disconnect. A cross-origin navigation
requires reconnecting. It does not import browser credentials or automate login.

1. In Forge desktop, open Settings → Browser and enable **Browser bridge**.
2. In Chrome or Edge's Extensions page, enable Developer mode and load this folder
   using **Load unpacked**. Choose Forge's bundled `browser-extension` folder
   (inside the installed app's `_internal` folder). Copy the generated extension ID.
3. In Forge, choose **Register native host** and paste that ID. Forge locates the
   bundled `ForgeBrowserHost.exe` automatically. Register each browser's extension
   ID if Chrome and Edge display different IDs.
4. Click the Forge extension button on a tab you want to connect. **ON** means the
   tab is selected; **!** means the native bridge is unavailable. Open Forge and
   click once to reconnect after a disconnection. Click again to disconnect.

Source builds can produce the host with:

```powershell
python -m PyInstaller --onefile --name ForgeBrowserHost browser_tools.py
```

Native host registration writes only the current user's Chrome/Edge native
messaging registry keys. Only explicitly registered extension IDs are accepted.
Each native-host session gets a separate tab namespace, so Chrome and Edge cannot
collide even if their numeric tab IDs match. Agent actions require a fresh
inspection in the same run. Password/file fields and stale or disabled targets
are rejected; a dispatched action is never automatically repeated after Stop.

No Chromium download is needed for this extension or Forge's native Browser
panel. Optional isolated Playwright browsing is a separate connection.
