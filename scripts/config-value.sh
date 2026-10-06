#!/usr/bin/env bash
# Read a setting (or report field) without shell evaluation or credential values.
set -euo pipefail
config_json=$(uv run downloadinux config show --json)
python3 -c '
import json, sys
report = json.load(sys.stdin)
key = sys.argv[1]
settings = {row["key"]: row["value"] for row in report["settings"]}
if key in settings:
    value = settings[key]
else:
    value = report
    for part in key.split("."):
        value = value[part]
print(str(value).lower() if isinstance(value, bool) else value)
' "$1" <<<"$config_json"
