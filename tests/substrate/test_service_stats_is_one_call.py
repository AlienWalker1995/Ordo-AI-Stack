"""The service-pressure endpoint must sample every container in ONE call, not N sequential ones.

The dashboard's hw-stat service-pressure bar calls `/stats/services`. Sampling a container costs
~1-2s because the daemon reads cgroup counters twice to compute a CPU delta, so a per-container
loop over this stack's ~24 running services took ~48s. It blew the dashboard's timeout and every
service rendered `running: false` — a panel confidently reporting a dead stack.

ops-api fixed that by fanning the samples out over a thread pool. `DockerBackend` reaches the same
place with one `docker stats --no-stream`, which samples them all at once. This pins that shape:
the cost must not scale with the number of containers.
"""
import ast
import inspect
import re
import textwrap

from ordo.broker import DockerBackend


def test_service_stats_issues_a_single_docker_stats_call():
    source = inspect.getsource(DockerBackend.service_stats)
    assert '"docker", "stats", "--no-stream"' in source, (
        "service_stats must sample every container in one call; a per-container loop scales at "
        "N x ~2s and blows the dashboard's timeout"
    )
    tree = ast.parse(textwrap.dedent(source))
    for loop in [n for n in ast.walk(tree) if isinstance(n, ast.For | ast.AsyncFor)]:
        calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)]
        assert not any(
            isinstance(c.func, ast.Attribute) and c.func.attr == "run" for c in calls
        ), "a subprocess call inside a loop over containers is the N x 2s shape"


def test_service_stats_has_a_timeout_that_allows_a_real_sample():
    """A short timeout plus a zero-filled fallback is how the lying panel happened."""
    source = inspect.getsource(DockerBackend.service_stats)
    timeouts = [int(t) for t in re.findall(r"timeout=(\d+)", source)]
    assert timeouts and min(timeouts) >= 30, (
        f"service_stats timeout {timeouts} is too tight for a real docker stats sample"
    )
