#!/usr/bin/env python3
"""
Ordo-AI-Stack — Storage Purge
═════════════════════════════
Deletes *really old* generated content so data/ storage stays flat. The LLM
never decides what to delete — a Hermes cron just runs this script and relays
its report.

Retention policy (introduced in #51):
  - Job drafts (data/drafts/<YYYY-MM-DD>_<slug>/): delete > 60 days (analytics
    window floor). Age comes from the folder-name date prefix, NOT mtime
    (metrics rewrites meta.json and would reset mtime).
  - Unrendered reel drafts (data/drafts-pending/<YYYY-MM-DD>_<slug>/): delete > 3
    days. Each is a news story written for the next render; by then it is stale.
  - ComfyUI output: delete files > 21 days (mtime). It lives on the comfyui-output
    named volume, not in data/, so this bucket reads --comfyui-output-dir (default:
    $COMFYUI_OUTPUT_DIR, which the agent container sets to its mount of the volume,
    /workspace/data/comfyui-output; else data/comfyui-output).
  - ComfyUI input/temp (data/comfyui-storage/ComfyUI/{input,temp}): > 21 days.
  - Backups/snapshots (data/_backups/*, data/hermes/state-snapshots/*): keep
    newest 3 per location.

Usage:
  python3 storage_purge.py            # dry-run (default): report only
  python3 storage_purge.py --apply    # delete expired content, then report
  python3 storage_purge.py --json     # machine-readable output
  python3 storage_purge.py --apply --force   # bypass the safety cap (first run)
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── Pure policy functions (unit-tested; no filesystem side effects) ───────────

_DRAFT_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_")


def draft_expired(folder_name, now, max_age_days=60):
    """True if a draft folder ``<YYYY-MM-DD>_<slug>`` is older than max_age_days.

    Age is taken from the leading date in the folder name. Names without a valid
    leading date return False — we never delete content we cannot confidently date.
    """
    m = _DRAFT_DATE.match(folder_name)
    if not m:
        return False
    try:
        stamp = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         tzinfo=UTC)
    except ValueError:
        return False
    return (now - stamp).days > max_age_days


def is_older_than(mtime_epoch, now_epoch, max_age_days):
    """True if an epoch mtime is older than max_age_days before now."""
    return (now_epoch - mtime_epoch) > max_age_days * 86400


def backups_to_delete(names, keep=3):
    """Return the names beyond the newest ``keep``, by sortable timestamp name."""
    if len(names) <= keep:
        return []
    ordered = sorted(names)  # lexicographic == chronological for YYYYMMDD-... names
    return ordered[:-keep]


def exceeds_cap(del_items, bucket_items, del_bytes,
                max_frac=0.5, max_bytes=10 * 1024 ** 3):
    """Safety guard: True if a run would delete an alarming amount.

    Trips when deletions exceed ``max_frac`` of a bucket's items OR ``max_bytes``
    total — a signal that the policy mis-fired rather than a normal increment.
    """
    if del_bytes > max_bytes:
        return True
    if bucket_items > 0 and (del_items / bucket_items) > max_frac:
        return True
    return False


def within_root(path, root):
    """Path safety: True only if ``path`` resolves to a location inside ``root``."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


# ── I/O shell: plan (read-only) then execute (delete) ─────────────────────────

DRAFT_MAX_AGE_DAYS = 60
PENDING_DRAFT_MAX_AGE_DAYS = 3
COMFY_MAX_AGE_DAYS = 21
BACKUPS_KEEP = 3

COMFY_INPUT_DIRS = ["comfyui-storage/ComfyUI/input", "comfyui-storage/ComfyUI/temp"]
BACKUP_LOCATIONS = ["_backups", "hermes/state-snapshots"]


def _dir_size(path):
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _plan_drafts(root, now, max_age_days, folder="drafts"):
    base = root / folder
    targets, total, size = [], 0, 0
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            total += 1
            if draft_expired(d.name, now, max_age_days):
                targets.append(d)
                size += _dir_size(d)
    return {"targets": targets, "total": total, "bytes": size, "root": root}


def _plan_mtime(root, now, dirs, max_age_days):
    """Files older than max_age_days (mtime) under each of ``dirs``, all inside ``root``."""
    now_epoch = now.timestamp()
    targets, total, size = [], 0, 0
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            total += 1
            try:
                st = p.stat()
            except OSError:
                continue
            if is_older_than(st.st_mtime, now_epoch, max_age_days):
                targets.append(p)
                size += st.st_size
    return {"targets": targets, "total": total, "bytes": size, "root": root}


def _plan_keep_newest(root, locations, keep):
    targets, total, size = [], 0, 0
    for rel in locations:
        loc = root / rel
        if not loc.is_dir():
            continue
        names = [d.name for d in loc.iterdir() if d.is_dir()]
        total += len(names)
        for name in backups_to_delete(names, keep):
            d = loc / name
            targets.append(d)
            size += _dir_size(d)
    return {"targets": targets, "total": total, "bytes": size, "root": root}


def plan_purge(data_root, now, draft_days=DRAFT_MAX_AGE_DAYS,
               mtime_days=COMFY_MAX_AGE_DAYS, keep=BACKUPS_KEEP,
               pending_days=PENDING_DRAFT_MAX_AGE_DAYS, comfyui_output_dir=None):
    """Walk every bucket and return what *would* be deleted. No side effects.

    Each bucket carries the ``root`` its deletions must stay inside: ``data_root``, except the
    ComfyUI output bucket, which is ``comfyui_output_dir`` (default ``data_root/comfyui-output``).
    """
    output_dir = Path(comfyui_output_dir) if comfyui_output_dir else data_root / "comfyui-output"
    comfy_input_dirs = [data_root / rel for rel in COMFY_INPUT_DIRS]
    return {
        "drafts": _plan_drafts(data_root, now, draft_days),
        "drafts-pending": _plan_drafts(data_root, now, pending_days, folder="drafts-pending"),
        "comfyui-output": _plan_mtime(output_dir, now, [output_dir], mtime_days),
        "comfyui-input": _plan_mtime(data_root, now, comfy_input_dirs, mtime_days),
        "backups": _plan_keep_newest(data_root, BACKUP_LOCATIONS, keep),
    }


def execute_purge(plan, force=False):
    """Delete the planned targets, per-bucket cap-guarded unless ``force``.

    Every target is re-checked with ``within_root`` against its bucket's ``root``
    before unlinking, so deletions can never escape the directory a bucket covers.
    """
    results = {}
    for bucket, p in plan.items():
        targets, total, size, root = p["targets"], p["total"], p["bytes"], p["root"]
        if exceeds_cap(len(targets), total, size) and not force:
            results[bucket] = {"deleted": 0, "freed": 0, "aborted": True,
                               "would_delete": len(targets), "would_free": size}
            continue
        deleted, freed = 0, 0
        for t in targets:
            if not within_root(t, root):
                continue
            try:
                if t.is_dir():
                    sz = _dir_size(t)
                    shutil.rmtree(t)
                else:
                    sz = t.stat().st_size
                    t.unlink()
                deleted += 1
                freed += sz
            except OSError:
                pass
        results[bucket] = {"deleted": deleted, "freed": freed, "aborted": False}
    return results


# ── Reporting + CLI ───────────────────────────────────────────────────────────

def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def render_report(plan, applied, data_root, now):
    """Build a Discord-ready ASCII report (no compound/zero-width emoji)."""
    mode = "APPLY" if applied is not None else "DRY-RUN"
    lines = [f"# Storage Purge ({mode}) - {now:%Y-%m-%d %H:%M UTC}", ""]
    total_freed = 0
    aborted_any = False
    for bucket, p in plan.items():
        n, size = len(p["targets"]), p["bytes"]
        if applied is not None:
            r = applied[bucket]
            if r["aborted"]:
                aborted_any = True
                lines.append(f"[SAFETY-ABORT] {bucket}: would delete {n} item(s) "
                             f"({_human(size)}) - over cap, skipped. Re-run with --force.")
                continue
            total_freed += r["freed"]
            lines.append(f"[{bucket}] deleted {r['deleted']} item(s), freed {_human(r['freed'])}")
        else:
            total_freed += size
            note = "  (OVER CAP - would skip without --force)" if exceeds_cap(n, p["total"], size) else ""
            lines.append(f"[{bucket}] {n} of {p['total']} item(s) eligible, {_human(size)}{note}")
    lines.append("")
    verb = "Freed" if applied is not None else "Reclaimable"
    lines.append(f"{verb}: {_human(total_freed)}")
    if aborted_any:
        lines.append("One or more buckets hit the safety cap and were skipped.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Ordo-AI-Stack storage purge")
    parser.add_argument("--apply", action="store_true", help="Delete (default: dry-run)")
    parser.add_argument("--force", action="store_true", help="Bypass the per-bucket safety cap")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--data-root", default=None, help="Override data/ root (testing)")
    parser.add_argument("--comfyui-output-dir", default=os.environ.get("COMFYUI_OUTPUT_DIR") or None,
                        help="ComfyUI output dir (the comfyui-output volume's mount; "
                             "default: $COMFYUI_OUTPUT_DIR, else <data-root>/comfyui-output)")
    args = parser.parse_args()

    data_root = Path(args.data_root) if args.data_root else Path(__file__).resolve().parent.parent / "data"
    now = datetime.now(UTC)

    plan = plan_purge(data_root, now, comfyui_output_dir=args.comfyui_output_dir)
    applied = execute_purge(plan, force=args.force) if args.apply else None

    if args.json:
        out = {
            "mode": "apply" if args.apply else "dry-run",
            "timestamp": now.isoformat(),
            "buckets": {
                b: {"eligible": len(p["targets"]), "total": p["total"], "bytes": p["bytes"]}
                for b, p in plan.items()
            },
        }
        if applied is not None:
            out["applied"] = applied
        print(json.dumps(out, indent=2))
    else:
        print(render_report(plan, applied, data_root, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
