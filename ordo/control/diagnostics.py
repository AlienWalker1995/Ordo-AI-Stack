"""Host diagnostics the control plane reports: processes stuck in uninterruptible sleep.

One whose kernel wait channel (`wchan`) names a p9 function is blocked on a 9p mount (a Windows
host path under WSL2); `p9_wedged` lists those apart.
"""
from __future__ import annotations

import subprocess
from typing import Any


def dstate() -> dict[str, Any]:
    """Report uninterruptible-sleep (D-state) processes across running containers."""
    wedged = []
    scanned = 0
    errors = []
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        container_names = [n.strip() for n in proc.stdout.splitlines() if n.strip()]
        for name in container_names:
            scanned += 1
            try:
                top = subprocess.run(
                    ["docker", "top", name, "-eo", "pid,stat,wchan:40,comm"],
                    capture_output=True, text=True, timeout=10,
                )
                lines = top.stdout.strip().splitlines()
                if len(lines) < 2:
                    continue
                # Parse header to find column indices
                header = lines[0].split()
                try:
                    pid_idx = header.index("PID")
                    stat_idx = header.index("STAT")
                    wchan_idx = header.index("WCHAN")
                    comm_idx = header.index("COMMAND")
                except ValueError:
                    errors.append(f"{name}: unexpected ps columns {header}")
                    continue
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) < len(header):
                        continue
                    stat = parts[stat_idx]
                    if not stat.startswith("D"):
                        continue
                    wchan = parts[wchan_idx]
                    wedged.append({
                        "container": name,
                        "pid": parts[pid_idx],
                        "stat": stat,
                        "wchan": wchan,
                        "comm": parts[comm_idx],
                        "p9": "p9" in wchan,
                    })
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    except Exception as exc:
        errors.append(f"docker ps failed: {exc}")
    return {
        "scanned": scanned,
        "wedged": wedged,
        "p9_wedged": [w for w in wedged if w["p9"]],
        "errors": errors,
    }
