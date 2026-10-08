#!/bin/sh
# Inside the converter image: run the official converter on the CPU over inputs run.sh already
# downloaded and checked into /work, write the artifact into /out and record its sha256.
# Usage: convert.sh <artifact name>, reading /inputs/<name>.args (one converter argument per line),
# or /inputs/<name>.upgrade for a published v2 artifact: upstream's offline upgrade tool
# (tools/upgrade_ninfer_v2_to_v3.py, standard library only, weight bytes preserved) rewrites it as v3.
set -eu

name="${1:?usage: convert.sh <artifact name>}"
upgrade_file="/inputs/${name}.upgrade"
if [ -f "$upgrade_file" ]; then
    # Two non-comment lines: the v2 input under /work, then the v3 output under /out.
    set -- $(grep -v '^#' "$upgrade_file" | grep -v '^[[:space:]]*$')
    [ "$#" -eq 2 ] || { echo "$upgrade_file must name exactly an input and an output" >&2; exit 2; }
    if [ -e "$2" ]; then
        echo "$2 already exists: move it away to upgrade again" >&2
        exit 2
    fi
    python3 /ninfer/tools/upgrade_ninfer_v2_to_v3.py "$1" "$2"
    sha256sum "$2" | tee "$2.sha256"
    exit 0
fi
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
