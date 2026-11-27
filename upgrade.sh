#!/usr/bin/env bash
set -eo pipefail
checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$checkout/deploy/upgrade.py" --checkout "$checkout" "$@"
