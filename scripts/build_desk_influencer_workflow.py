"""Build LTX-2.3 Desk Influencer workflow from the street-interview clone.

Applies parameter-level mutations only. Structural I2V additions are
performed by the operator in ComfyUI's UI per the accompanying README.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build(source: Path, target: Path) -> None:
    data = json.loads(source.read_text(encoding="utf-8"))
    # Mutations are layered in by later tasks. Step 3 just clones.
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    args = p.parse_args()
    build(args.source, args.target)
    print(f"wrote {args.target}")


if __name__ == "__main__":
    main()
