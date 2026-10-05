#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d --build
echo "Open http://localhost:8081. Download a model in Manage models."
