# Third-party notices

Forge source is licensed under the MIT license in `LICENSE`. Third-party software
retains its own copyright notices, licenses and conditions. Model weights and
imported plugins retain the licenses supplied by their respective publishers.

Windows binary packages include a `third-party-licenses` directory generated from
installed dependency distributions and frontend packages. Source builds can
generate the same notice bundle with `python scripts/collect_licenses.py`.

| Component | License | Source |
| --- | --- | --- |
| Python | PSF | https://www.python.org/psf/license/ |
| React / React DOM | MIT | https://github.com/facebook/react |
| Lucide | ISC | https://github.com/lucide-icons/lucide |
| react-markdown | MIT | https://github.com/remarkjs/react-markdown |
| TypeScript | Apache-2.0 | https://github.com/microsoft/TypeScript |
| Vite / Vitest | MIT | https://github.com/vitejs/vite / https://github.com/vitest-dev/vitest |
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| HTTPX / Uvicorn | BSD-3-Clause | https://github.com/encode/httpx / https://github.com/encode/uvicorn |
| MCP Python SDK | MIT | https://github.com/modelcontextprotocol/python-sdk |
| Playwright | Apache-2.0 | https://github.com/microsoft/playwright |
| PyYAML | MIT | https://github.com/yaml/pyyaml |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| PyInstaller | GPL with bootloader exception | https://github.com/pyinstaller/pyinstaller |
| pyperclip / pywinauto | BSD | https://github.com/asweigart/pyperclip / https://github.com/pywinauto/pywinauto |
| pystray | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| sounddevice / faster-whisper | MIT | https://github.com/spatialaudio/python-sounddevice / https://github.com/SYSTRAN/faster-whisper |
| DDGS | MIT | https://github.com/deedy5/ddgs |

Forge uses the unmodified pystray library. Its source is available from its linked
upstream repository. Forge's public source and build instructions permit rebuilding
the application with a modified library; its LGPL/GPL texts are included in the
binary notice bundle. PyInstaller's bootloader exception applies to generated
executables; it does not change the licenses of bundled dependencies.

WebView2 is a Microsoft runtime installed separately. Chromium, Node.js, CUDA,
Docker engine images, system libraries and downloaded speech/model weights carry
their own notices in their distributions. Optional runtime downloads are not
relicensed by Forge. No model weights, third-party plugin source or personal data
are included in this Git repository.
