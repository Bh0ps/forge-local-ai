# syntax=docker/dockerfile:1
FROM node:24.14.0-bookworm-slim@sha256:d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8 AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/src ./src
COPY frontend/public ./public
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
RUN npm run build

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FORGE_DATA_DIR=/data FORGE_PROJECTS_ROOT=/workspace BIND_HOST=0.0.0.0 \
    FORGE_BROWSER_HEADLESS=1 PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    NPM_CONFIG_CACHE=/data/cache/npm
WORKDIR /app
COPY requirements.txt requirements-integrations.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libportaudio2 \
    && pip install --no-cache-dir -r requirements.txt -r requirements-integrations.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home forge \
    && mkdir -p /data /workspace && chown forge:forge /data /workspace
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY *.py ./
COPY assets ./assets
COPY --from=frontend /build/frontend/dist ./frontend/dist
USER forge
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/v1/health', timeout=3)"
CMD ["python", "model_manager.py"]
