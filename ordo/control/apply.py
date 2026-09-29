"""The post-render step: bring the running stack to the current render, and commit a source edit.

`apply_render` recreates exactly the changed set (ordo/render/changed_set.py, what the host's
`ordo apply` computes), GPU-lease checked, netns members with their owner, and names what only the
host can finish. `commit` is the one write path every source edit takes: write the source, write
its render to out/, apply it, and on a refused or failed apply restore the previous source and
re-apply that.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..render.changed_set import STOPPED_STATES, Change, diff_services, stale_one_shot_jobs
from ..render.models_volume import required_model_files
from ..render.open_webui_probe import OPEN_WEBUI_PROBE, OPEN_WEBUI_SERVICE, open_webui_verdict
from ..render.stack import lifecycle_group, plan_named
from .broker import SELF_REFERENTIAL_SERVICES, Broker
from .lifecycle import LeaseGuard
from .responses import confirmed, error
from .source import StackSource, enabled_ids


class RenderApply:
    """Recreates what a render changed, through the broker's container backend."""

    def __init__(self, source: StackSource, broker: Broker | None, lease: LeaseGuard,
                 model_volume_files: Callable[[], set[str] | None] | None):
        self.source = source
        self.broker = broker
        self.lease = lease
        # Lists the file names in the models volume (None: it could not be listed); None = no volume
        # to check (a control plane without the Docker socket, and tests that do not wire one).
        self.model_volume_files = model_volume_files

    def commit(self, text: str, rendered: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Write `text` as the operator source, write its render to out/, then apply it.

        Returns (the apply result, None), or (None, a failure payload) when the apply refused or
        failed: the previous source is then written back, re-rendered and re-applied, so the source
        never names a config the stack is not running. The apply result is None when this control
        plane has no container backend (a hand-run or test instance): nothing is recreated then.
        """
        previous_text = self.source.read_text()
        self.source.write(text)
        rendered.write(self.source.out_dir)
        if not self.broker:
            return None, None
        # A service the new render no longer defines (a disabled plugin's) is stopped by the apply.
        applied = self.apply_render()
        if "_status" not in applied:
            return applied, None
        self.source.write(previous_text)
        self.source.render().write(self.source.out_dir)
        rollback = self.apply_render()
        if rollback.pop("_status", None) is None:
            outcome = "ordo.yaml and out/ were rolled back and the previous render re-applied"
        else:
            outcome = (f"ordo.yaml and out/ were rolled back, but re-applying the previous render failed "
                       f"too ({rollback.get('error')}); run `ordo apply` on the host")
        applied["error"] = f"{applied['error']}; {outcome}"
        applied["rolled_back"] = True
        applied["rollback"] = rollback
        return None, applied

    def _secret_holds(self, rc: Any) -> dict[str, list[str]]:
        """service -> the required secrets out/secrets.env lacks, for each enabled plugin's services
        (its compose services and its MCP server's). Started without them they crash-loop, so the
        post-render step leaves them to the host (`ordo secrets set`, then `ordo apply`)."""
        present = self.source.secrets_present()
        holds: dict[str, list[str]] = {}
        for plugin_id in sorted(enabled_ids(rc)):
            plugin = self.source.registry.get(plugin_id)
            if plugin is None:
                continue
            missing = [key for key in plugin.secrets if key not in plugin.optional_secrets and key not in present]
            if not missing:
                continue
            names = [service.name for service in plugin.services]
            names += [server["service"] for server in rc.mcp_servers
                      if server.get("plugin_id") == plugin_id and not server.get("hosted")]
            for name in names:
                held = holds.setdefault(name, [])
                held += [key for key in missing if key not in held]
        return holds

    def _model_file_holds(self, services: list[str], doc: dict[str, Any], rc: Any) -> dict[str, str]:
        """service -> why it waits for the host, for each of `services` that loads a model file the
        models volume lacks. Recreated onto a missing file it crash-loops; the host's `ordo apply
        --only <service>` fetches the file first (a download of that size does not belong in this
        process; the model switch refuses on the same check). A volume that cannot be listed holds
        every loader."""
        if self.model_volume_files is None:
            return {}
        needed = [need for need in required_model_files(doc, rc.env, services) if not need.optional]
        if not needed:
            return {}
        present = self.model_volume_files()
        holds: dict[str, list[str]] = {}
        for need in needed:
            if present is None or need.file not in present:
                holds.setdefault(need.service, []).append(need.file)
        if present is None:
            return {name: "cannot list the models volume to confirm its model files are in place"
                    for name in holds}
        return {name: (f"loads {', '.join(files)}, which the models volume lacks: `ordo apply --only "
                       f"{name}` on the host fetches it first") for name, files in holds.items()}

    def _unbuilt_image_holds(self, services: list[str], rendered: dict[str, Any], rc: Any) -> dict[str, str]:
        """service -> why it waits for the host, for each of `services` whose first-party image (built
        from this checkout by `ordo build`, never published to a registry) is not in the local image
        cache. Compose would try to pull it and fail; the host's `ordo apply --only <service>` builds
        it first. A third-party image the cache lacks is left to compose, which pulls it."""
        owned = set(rc.first_party_images)
        holds: dict[str, str] = {}
        for name in services:
            service = rendered.get(name)
            if service is None or service.image_id is not None:
                continue
            repo = service.image_ref.rsplit(":", 1)[0]
            if repo in owned:
                holds[name] = (f"image {service.image_ref} is built from this checkout and is not in the "
                               f"local image cache: `ordo apply --only {name}` on the host builds it first")
        return holds

    def _host_reasons(self, changes: list[Change], doc: dict[str, Any], rc: Any,
                      rendered: dict[str, Any]) -> dict[str, str]:
        """Why each changed service this process must not recreate is left to the host."""
        secret_holds = self._secret_holds(rc)
        changed = [change.service for change in changes]
        file_holds = self._model_file_holds(changed, doc, rc)
        image_holds = self._unbuilt_image_holds(changed, rendered, rc)
        reasons: dict[str, str] = {}
        for change in changes:
            name = change.service
            if name in SELF_REFERENTIAL_SERVICES:
                reasons[name] = (f"{name} runs the control plane (or is the agent calling it) and cannot be "
                                 f"recreated through it ({'; '.join(change.reasons)})")
            elif change.incomparable:
                reasons[name] = "; ".join(change.reasons)
            elif name in secret_holds:
                reasons[name] = (f"needs secret(s) {', '.join(secret_holds[name])}, which out/secrets.env does "
                                 "not hold: `ordo secrets set <KEY>` on the host first")
            elif name in image_holds:
                reasons[name] = image_holds[name]
            elif name in file_holds:
                reasons[name] = file_holds[name]
            else:
                members = [m for m in lifecycle_group(doc, name)[1:] if m in SELF_REFERENTIAL_SERVICES]
                if members:
                    reasons[name] = f"its netns member(s) {', '.join(members)} run the control plane"
        return reasons

    def apply_render(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Bring the stack to the current render: recreate exactly the changed set.

        The changed set is every long-running rendered service whose config hash or image differs
        from its container's, or that has none (ordo/render/changed_set.py, what the host's `ordo
        apply` computes). It is recreated in one `up -d --no-deps --force-recreate` call with each
        owner's netns members, after the GPU-lease check every lifecycle verb makes (an evicted
        resident is refused, 409). A service the render no longer defines that is not already stopped is stopped
        (`stopped`) unless it is already stopped (`orphans`). A one-shot job's stopped container the render moved past is removed,
        never started (`removed_jobs`; `run --rm` creates a fresh one), and a running one is left
        alone (`running_jobs`), as the host's `ordo apply` does. Left to the host, and named with the
        command that finishes the job: the control plane itself and the agent calling it, a container another compose version
        created (its hash is not comparable), a service whose secrets are missing, one whose
        first-party image is not built yet, and one that would load a model file the models volume
        lacks (the host's apply builds and fetches them). Fails closed:
        a state that cannot be read refuses (503) and recreates nothing.

        `warnings` lists what the apply could not vouch for, without undoing it: when open-webui was
        recreated, the probe `ordo doctor` runs after the host's apply (ordo/render/open_webui_probe.py)
        runs in it once, and a failing verdict, or a probe that could not run, is one entry.
        """
        if not self.broker:
            return error(503, "no container backend: this control plane cannot read or recreate containers")
        try:
            state = self.broker.backend.stack_state()
            doc = self.broker.backend.rendered_compose()
        except Exception as e:  # noqa: BLE001 - any unreadable side means the changed set is unknown
            return error(503, f"cannot read what is rendered and what is running ({e}); "
                              "nothing was recreated")
        changes = diff_services(state.rendered, state.running, compose_version=state.compose_version)
        jobs = stale_one_shot_jobs(state.rendered, state.running, compose_version=state.compose_version)
        removed_jobs = [job.service for job in jobs if job.removable]
        host_reasons = self._host_reasons(changes, doc, self.source.render(), state.rendered)
        to_recreate = [change.service for change in changes if change.service not in host_reasons]
        _args, targets = plan_named(doc, to_recreate, force_recreate=True)
        # Every service the render no longer defines that is not already stopped is stopped (the host's `ordo apply`
        # does the same); one already stopped is left as an orphan.
        unrendered = set(state.running) - set(state.rendered)
        stopped = sorted(name for name in unrendered if state.running[name].state not in STOPPED_STATES)
        host = sorted(host_reasons)
        plan: dict[str, Any] = {
            "dry_run": dry_run,
            "changes": [{"service": change.service, "reasons": list(change.reasons)} for change in changes],
            "recreated": sorted(targets),
            "stopped": stopped,
            "removed_jobs": removed_jobs,
            "running_jobs": [job.service for job in jobs if not job.removable],
            "restart_required_on_host": host,
            "host_reasons": host_reasons,
            "host_command": f"ordo apply --only {' '.join(host)}" if host else None,
            # Containers the render no longer defines that were already stopped.
            "orphans": sorted(unrendered - set(stopped)),
            "warnings": [],
        }
        conflict = self.lease.group_conflict(sorted(targets))
        if conflict:
            return {**conflict, "changes": plan["changes"]}
        if dry_run:
            return {"ok": True, **plan}
        try:
            for name in stopped:
                self.broker.backend.stop(name)
            if to_recreate:
                self.broker.backend.recreate_services(to_recreate)
            if removed_jobs:
                self.broker.backend.remove_stopped_containers(removed_jobs)
        except Exception as e:  # noqa: BLE001 - reported with the plan it was executing
            return error(500, f"applying the render failed: {e}", changes=plan["changes"])
        if OPEN_WEBUI_SERVICE in targets:
            ok, line = self.open_webui_verdict()
            if not ok:
                plan["warnings"].append(line)
        return {"ok": True, **plan}

    def open_webui_verdict(self) -> tuple[bool, str]:
        """(ok, one-line report) from the probe run inside the open-webui container, once, with no
        wait or retry (as `ordo doctor` runs it). A probe that cannot run is a failed verdict."""
        try:
            exit_code, output = self.broker.backend.exec_in_service(OPEN_WEBUI_SERVICE,
                                                                 ["python", "-c", OPEN_WEBUI_PROBE])
        except Exception as e:  # noqa: BLE001 - the container is already recreated; this only reports
            return False, f"! open-webui: could not verify its model-gateway connection ({type(e).__name__}: {e})"
        lines = output.strip().splitlines()
        if exit_code != 0:
            detail = lines[-1] if lines else f"exit {exit_code}"
            return False, f"! open-webui: could not verify its model-gateway connection (probe failed: {detail})"
        try:
            report = json.loads(lines[0]) if lines else None
        except ValueError:
            report = None
        if not isinstance(report, dict):
            return False, (f"! open-webui: could not verify its model-gateway connection "
                           f"(unreadable probe output: {output.strip()[:200]})")
        return open_webui_verdict(report)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /apply`: bring the stack to out/ as it is rendered now (after a host render, say).
        `{"dry_run": true}` returns the plan and changes nothing; otherwise `confirm` is required."""
        dry_run = bool(body.get("dry_run"))
        if not dry_run and not confirmed(body):
            return error(400, 'Destructive operation requires confirmation. Set {"confirm": true} in the '
                              'request body to proceed, or {"dry_run": true} for the plan.')
        return self.apply_render(dry_run=dry_run)
