#!/bin/sh
# Bootstrap uses only Docker and POSIX shell tools; no host Python is needed.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
python_image=$(awk '$1 == "FROM" { print $2; exit }' "$project_dir/Dockerfile")
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$project_dir,target=/workspace" \
  --workdir /workspace \
  --entrypoint python \
  "$python_image" scripts/init_env.py
