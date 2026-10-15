# Third-party notices

Forge source is licensed under the MIT license in `LICENSE`. Third-party software
retains its own copyright notices, licenses and conditions. Model weights and
imported plugins retain the licenses supplied by their respective publishers.

Windows binary packages include a `third-party-licenses` directory generated from
installed dependency distributions and frontend packages. Source builds can
generate the same notice bundle with `python scripts/collect_licenses.py`.
When a Python wheel omits its full license, collection uses reviewed, exact-version
upstream texts in `assets/third-party-licenses`. Their manifest pins byte hashes
and source provenance; collection is offline and fails if a full notice is
missing, altered or available only for another version. Package license identifiers
alone are not a substitute for the full texts.

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
| Hugging Face Hub | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| GGUF Python / llama.cpp | MIT | https://github.com/ggml-org/llama.cpp |
| Strata engine and selected tokenizer, template, pack and protocol support | MIT | https://github.com/mach10x/strata |
| NVIDIA CUDA runtime and cuBLAS redistributables | NVIDIA software license | https://docs.nvidia.com/cuda/eula/ |

| Playwright | Apache-2.0 | https://github.com/microsoft/playwright |
| PyYAML | MIT | https://github.com/yaml/pyyaml |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| PyInstaller | GPL with bootloader exception | https://github.com/pyinstaller/pyinstaller |
| pyperclip / pywinauto | BSD | https://github.com/asweigart/pyperclip / https://github.com/pywinauto/pywinauto |
| pystray | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| sounddevice / faster-whisper | MIT | https://github.com/spatialaudio/python-sounddevice / https://github.com/SYSTRAN/faster-whisper |
| DDGS | MIT | https://github.com/deedy5/ddgs |
| jsonschema | MIT | https://github.com/python-jsonschema/jsonschema |
| pypdf | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| openpyxl | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| python-docx | MIT | https://github.com/python-openxml/python-docx |
| ReportLab | BSD | https://www.reportlab.com/ |
| CTranslate2 | MIT | https://github.com/OpenNMT/CTranslate2 |
| FlatBuffers / Tokenizers | Apache-2.0 | https://github.com/google/flatbuffers / https://github.com/huggingface/tokenizers |
| Primp and included upstream notices | MIT; bundled rustls is Apache-2.0 OR MIT OR ISC | https://github.com/deedy5/primp |
| proxy_tools | BSD-style upstream text (two conditions); metadata says MIT | https://github.com/jtushman/proxy_tools |

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

Native Windows inference uses the exact artifacts and source identities in
`native-runtime.lock.json`. The bundled workers share CUDA 13.4 libraries within
the CUDA 13 major family. No system CUDA installation or driver installation is
performed. Full native notices accompany the binaries; Strata attribution and
the changes to its support modules are in `forge_vendor/strata/ORIGIN.json`.
Model weights are downloaded separately and retain their own publisher licenses.

The optional Strata Coder setup fetches its MTP tensors from the official pinned
Qwen3.8 Flash Next checkpoint. These weights use Qwen Community License 1.0,
including its separate licensing requirement for commercial coding/work assistants.
The exact notice accompanies Forge at `forge_vendor/strata/QWEN_MTP_LICENSE` and is
copied beside downloaded draft files. No Qwen weights are included in the installer.
