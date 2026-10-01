#!/bin/sh
# Inside the converter image: run the official converter on the CPU over inputs run.sh already
# downloaded and checked into /work, write the artifact into /out and record its sha256.
# Usage: convert.sh <artifact name>, reading /inputs/<name>.args (one converter argument per line).
set -eu

name="${1:?usage: convert.sh <artifact name>}"
args_file="/inputs/${name}.args"
[ -f "$args_file" ] || { echo "no converter arguments for $name under /inputs" >&2; exit 2; }

out="$(grep -v '^#' "$args_file" | grep -A1 -x -- '--out' | tail -n 1)"
[ -n "$out" ] || { echo "$args_file names no --out" >&2; exit 2; }
if [ -e "$out" ]; then
    echo "$out already exists: move it away to convert again" >&2
    exit 2
fi

# One argument per line, comments and blank lines skipped, handed to the converter verbatim.
set --
while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    set -- "$@" "$line"
done < "$args_file"

cd /ninfer
python3 -m tools.convert "$@"
sha256sum "$out" | tee "$out.sha256"
