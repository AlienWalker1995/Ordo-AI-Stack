#!/usr/bin/env bash
# Convert one artifact with the official NInfer converter, CPU only (no GPU is attached, so no GPU
# lease is involved). Usage: services/ninfer/convert/run.sh <artifact name> <work dir> <out dir>
#   <artifact name>  a pair of files in inputs/: <name>.sources (pinned inputs) and either <name>.args
#                    (a conversion) or <name>.upgrade (a published v2 artifact upgraded to v3)
#   <work dir>       receives the downloaded inputs (resumable; about 56 GB for a 27B BF16 source)
#   <out dir>        receives the .ninfer file and its .sha256
# The inputs are downloaded and checked on the host: a container writing through a bind mount is
# several times slower on Docker Desktop. CONVERT_CPUS caps the converter's CPUs (default 12, the
# host's sustained-load budget).
set -euo pipefail

name="${1:?usage: run.sh <artifact name> <work dir> <out dir>}"
work="${2:?usage: run.sh <artifact name> <work dir> <out dir>}"
out="${3:?usage: run.sh <artifact name> <work dir> <out dir>}"
here="$(cd "$(dirname "$0")" && pwd)"
sources="$here/inputs/$name.sources"
[ -f "$sources" ] && { [ -f "$here/inputs/$name.args" ] || [ -f "$here/inputs/$name.upgrade" ]; }     || { echo "no inputs named $name in $here/inputs" >&2; exit 2; }

commit="$(sed -n 's/^ARG NINFER_COMMIT=//p' "$here/Dockerfile")"
serving="$(sed -n 's/^ARG NINFER_COMMIT=//p' "$here/../Dockerfile")"
if [ -z "$commit" ] || [ "$commit" != "$serving" ]; then
    echo "the converter's NINFER_COMMIT ($commit) must equal services/ninfer's ($serving)" >&2
    exit 2
fi
image="ordo/ninfer-convert:${commit:0:12}"

# Every input: skip it when it is already in place with the pinned sha256, else download it (resuming
# a partial file) and refuse to go on unless the result matches.
mkdir -p "$work" "$out"
grep -v '^#' "$sources" | grep -v '^[[:space:]]*$' | while read -r sha path url; do
    target="$work/$path"
    if [ -f "$target" ] && echo "$sha  $target" | sha256sum -c --quiet - 2>/dev/null; then
        echo "verified $path"
        continue
    fi
    mkdir -p "$(dirname "$target")"
    case "$url" in
        inputs:*)
            # a file tracked beside the recipe (e.g. a tensor index generated from pinned headers)
            echo "copying $path"
            cp "$here/inputs/${url#inputs:}" "$target.part"
            ;;
        *)
            echo "downloading $path"
            curl -fL --retry 10 --retry-delay 10 --retry-all-errors -C - -o "$target.part" "$url"
            ;;
    esac
    if ! echo "$sha  $target.part" | sha256sum -c --quiet -; then
        echo "checksum mismatch for $path: the download was deleted" >&2
        rm -f "$target.part"
        exit 1
    fi
    mv "$target.part" "$target"
    echo "verified $path"
done

# The directory as Docker on this host spells it: `pwd -W` gives C:/... under Git Bash on Windows,
# and plain `pwd` is the path everywhere else.
host_path() { (cd "$1" && { pwd -W 2>/dev/null || pwd; }); }

docker build -t "$image" "$here"
MSYS_NO_PATHCONV=1 docker run --rm --cpus "${CONVERT_CPUS:-12}" \
    --mount "type=bind,src=$(host_path "$work"),dst=/work,readonly" \
    --mount "type=bind,src=$(host_path "$out"),dst=/out" \
    "$image" "$name"
