# Forge browser bridge

The extension uses `activeTab`, so it sees only a tab explicitly connected by
clicking its toolbar button. Click again to disconnect. A cross-origin navigation
requires reconnecting. It does not import browser credentials or automate login.

1. Install Forge's optional integrations and enable **Browser bridge** in Settings.
2. In Chrome or Edge's Extensions page, enable Developer mode and load this folder
   using **Load unpacked**. Copy the generated extension ID.
3. In Forge, choose **Register native host**, paste that ID and select the
   `ForgeBrowserHost.exe` included in the Windows package.
4. Click the Forge extension button on a tab you want to connect. **ON** means the
   tab is selected; **!** means the native bridge is unavailable. Open Forge and
   click the button again to reconnect.

Source builds can produce the host with:

```powershell
python -m PyInstaller --onefile --name ForgeBrowserHost browser_tools.py
```

Native host registration writes only the current user's Chrome/Edge native
messaging registry keys. The manifest permits the specified extension ID only.
For isolated automation, install Chromium with `python -m playwright install chromium`.
