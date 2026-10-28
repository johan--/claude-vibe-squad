#!/bin/bash
# Remove unused uv cache entries; uv keeps installed tools and environments.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    echo "uv-cache-prune: uv is unavailable" >&2
    exit 1
fi
exec uv cache prune
