"""Dependency-free canonical gpu-assignments YAML formatter.

Importable by both pydantic-land (model_registry / ops-controller main) and the
host-side setup script (scripts/detect_hardware.py) without pulling pydantic or
any other ops-controller-specific dependency.

Canonical implementation — all three emitters in the stack delegate here.
"""
import re


def render_gpu_assignments_yaml(assignments: dict) -> str:
    """Canonical emitter — both CUDA_VISIBLE_DEVICES (WSL2-effective) and device_ids
    (native-Linux). Replaces the duplicated format_gpu_assignments / render_gpu_assignments."""
    lines = [
        "# Machine-local GPU pins. Generated from the model registry — edit via the dashboard GPU view.",
        "# Both layers (CUDA_VISIBLE_DEVICES + device_ids) are required (see detect_hardware.py / WSL2).",
        "services:",
    ]
    for service, uuid in assignments.items():
        if not uuid:
            continue
        lines += [
            f"  {service}:",
            "    environment:",
            f"      - CUDA_VISIBLE_DEVICES={uuid}",
            f"      - NVIDIA_VISIBLE_DEVICES={uuid}",
            "    deploy:",
            "      resources:",
            "        reservations:",
            "          devices:",
            "            - driver: nvidia",
            f"              device_ids: ['{uuid}']",
            "              capabilities: ['gpu']",
        ]
    return "\n".join(lines) + "\n"


def parse_gpu_assignments_yaml(text: str) -> dict:
    """Parse the fixed-format gpu-assignments.yml into {service: uuid}.
    Accepts both single- and double-quoted UUIDs so legacy files (double-quoted
    from the old detect_hardware emitter) and new files (single-quoted) both parse."""
    result: dict = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^  (\S+):\s*$", line)
        if m:
            current = m.group(1)
            continue
        m = re.search(r"device_ids:\s*\[['\"]([^'\"]+)['\"]\]", line)
        if m and current:
            result[current] = m.group(1)
            current = None
    return result
