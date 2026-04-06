"""Workflow management for loading and processing ComfyUI workflows"""

import copy
import json
import logging
import random
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

from models.workflow import WorkflowParameter, WorkflowToolDefinition

logger = logging.getLogger("MCP_Server")


class DefaultsProvider(Protocol):
    def get_default(self, namespace: str, key: str, fallback: Any) -> Any: ...

PLACEHOLDER_PREFIX = "PARAM_"
PLACEHOLDER_TYPE_HINTS = {
    "STR": str,
    "STRING": str,
    "TEXT": str,
    "INT": int,
    "FLOAT": float,
    "BOOL": bool,
}
PLACEHOLDER_DESCRIPTIONS = {
    "prompt": "Main text prompt used inside the workflow.",
    "style_prompt": "Style and instrumentation prompt for the song.",
    "seed": "Random seed for image generation. If not provided, a random seed will be generated.",
    "width": "Image width in pixels. Default: 512.",
    "height": "Image height in pixels. Default: 512.",
    "model": "Checkpoint model name (e.g., 'v1-5-pruned-emaonly.ckpt', 'sd_xl_base_1.0.safetensors'). Default: 'v1-5-pruned-emaonly.ckpt'.",
    "steps": "Number of sampling steps. Higher = better quality but slower. Default: 20.",
    "cfg": "Classifier-free guidance scale. Higher = more adherence to prompt. Default: 8.0.",
    "sampler_name": "Sampling method (e.g., 'euler', 'dpmpp_2m', 'ddim'). Default: 'euler'.",
    "scheduler": "Scheduler type (e.g., 'normal', 'karras', 'exponential'). Default: 'normal'.",
    "denoise": "Denoising strength (0.0-1.0). Default: 1.0.",
    "negative_prompt": "Negative prompt to avoid certain elements. Default: 'text, watermark'.",
    "tags": "Comma-separated descriptive tags for the audio model.",
    "lyrics": "Full lyric text that should drive the audio generation.",
    "seconds": "Audio duration in seconds. Default: 60 (1 minute).",
    "lyrics_strength": "How strongly lyrics influence audio generation (0.0-1.0). Default: 0.99.",
    "language": "Language for the generated vocals. Default: 'en'.",
    "key": "Musical key for the song. Default: 'C major'.",
    "duration": "Video duration in seconds. Default: 5.",
    "fps": "Frames per second for video output. Default: 24.",
    "frames": "Number of video frames (must be divisible by 8+1 for LTX-2). Default: 121 (5s at 24fps).",
    "scheduler": "Scheduler type for the sampler. Default: 'simple'.",
}
OPTIONAL_PARAM_DEFAULTS = {
    "width": 512,
    "height": 512,
    "model": "v1-5-pruned-emaonly.ckpt",
    "steps": 20,
    "cfg": 8.0,
    "sampler_name": "euler",
    "scheduler": "normal",
    "denoise": 1.0,
    "negative_prompt": "text, watermark",
    "seconds": 60,
    "lyrics_strength": 0.99,
    "language": "en",
    "key": "C major",
    "scheduler": "simple",
    "duration": 5,
    "fps": 24,
    "frames": 121,
}
DEFAULT_OUTPUT_KEYS = ("images", "image", "gifs", "gif")
AUDIO_OUTPUT_KEYS = ("audio", "audios", "sound", "files")
VIDEO_OUTPUT_KEYS = ("videos", "video", "mp4", "mov", "webm")


class WorkflowManager:
    def __init__(self, workflows_dir: Path):
        self.workflows_dir = Path(workflows_dir).resolve()
        self._tool_names: set[str] = set()
        self._workflow_cache: dict[str, dict[str, Any]] = {}
        self._workflow_mtime: dict[str, float] = {}  # Track file modification times for cache invalidation
        self.tool_definitions = self._load_workflows()

    @staticmethod
    def is_ui_workflow_export(workflow: dict[str, Any]) -> bool:
        """True if JSON is ComfyUI's visual editor format (not /prompt API format)."""
        nodes = workflow.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return False
        n0 = nodes[0]
        return isinstance(n0, dict) and "type" in n0 and "class_type" not in n0

    def _safe_workflow_path_under_root(self, workflow_id: str, root: Path) -> Path | None:
        """Resolve workflow ID under a single root (path traversal safe)."""
        root = root.resolve()
        raw = workflow_id.strip().replace("\\", "/")
        if not raw or raw.startswith("/"):
            return None
        if ".." in raw.split("/"):
            logger.warning("Path traversal attempt in workflow_id: %s", workflow_id)
            return None

        if "/" in raw:
            rel = raw[:-5] if raw.lower().endswith(".json") else raw
            workflow_path = (root / rel).with_suffix(".json").resolve()
        else:
            safe_id = raw.replace("..", "_")
            safe_id = "".join(c for c in safe_id if c.isalnum() or c in ("_", "-"))
            if not safe_id:
                logger.warning("Invalid workflow_id after sanitization: %s", workflow_id)
                return None
            workflow_path = (root / f"{safe_id}.json").resolve()

        try:
            workflow_path.relative_to(root)
        except ValueError:
            logger.warning("Path traversal attempt detected: %s", workflow_id)
            return None

        return workflow_path if workflow_path.is_file() else None

    def _safe_workflow_path(self, workflow_id: str) -> Path | None:
        """Resolve workflow ID to file path with path traversal protection.

        Supports:
        - **Flat** ids: ``generate_image`` → ``<workflows_dir>/generate_image.json``
        - **Nested** ids: ``mcp-api/generate_image`` → relative path under workflows_dir
        """
        return self._safe_workflow_path_under_root(workflow_id, self.workflows_dir)
    
    def _load_workflow_metadata(self, workflow_path: Path) -> dict[str, Any]:
        """Load sidecar metadata if present.

        Prefer ``.wfmeta`` — it is not a ``*.json`` file, so ComfyUI's workflow
        browser (which lists every ``*.json`` under mcp-api) does not show MCP
        metadata as a fake workflow tab. Legacy ``.meta.json`` is still supported.
        """
        for metadata_path in (
            workflow_path.with_suffix(".wfmeta"),
            workflow_path.with_suffix(".meta.json"),
        ):
            if metadata_path.exists():
                try:
                    with open(metadata_path, encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(
                        "Failed to load metadata for %s from %s: %s",
                        workflow_path.name,
                        metadata_path.name,
                        e,
                    )
        return {}

    def list_workflow_ids(self) -> list[str]:
        """Every workflow id (relative POSIX path, no .json) under workflows_dir — no JSON parsing."""
        if not self.workflows_dir.exists():
            return []
        out: list[str] = []
        for workflow_path in sorted(self.workflows_dir.rglob("*.json")):
            if workflow_path.name.endswith(".meta.json"):
                continue
            rel = workflow_path.relative_to(self.workflows_dir)
            out.append(str(rel.with_suffix("")).replace("\\", "/"))
        return out

    def get_workflow_catalog(self) -> list[dict[str, Any]]:
        """Get catalog of all available workflows"""
        catalog = []
        if not self.workflows_dir.exists():
            return catalog

        paths = sorted(self.workflows_dir.rglob("*.json"))
        for workflow_path in paths:
            if workflow_path.name.endswith(".meta.json"):
                continue

            rel = workflow_path.relative_to(self.workflows_dir)
            workflow_id = str(rel.with_suffix("")).replace("\\", "/")
            try:
                with open(workflow_path, encoding="utf-8") as f:
                    workflow = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Skipping {workflow_path.name}: {e}")
                continue
            
            # Load metadata
            metadata = self._load_workflow_metadata(workflow_path)
            
            # Extract parameters
            parameters = self._extract_parameters(workflow)
            parameters = self._merge_metadata_parameters(parameters, metadata)
            available_inputs = {
                name: {
                    "type": param.annotation.__name__,
                    "required": param.required,
                    "description": param.description
                }
                for name, param in parameters.items()
            }
            # Ordo AI Stack: literal API JSON (valid in ComfyUI UI) has no PARAM_ placeholders.
            # Sidecar .wfmeta (or legacy .meta.json) supplies override_mappings + available_inputs for MCP catalog.
            if not available_inputs and metadata.get("available_inputs"):
                available_inputs = metadata["available_inputs"]
            
            # Get workflow defaults from metadata or infer from workflow_id
            workflow_defaults = metadata.get("defaults", {})
            if not workflow_defaults:
                workflow_defaults = self._infer_defaults(parameters)

            catalog.append({
                "id": workflow_id,
                "name": metadata.get("name", workflow_id.replace("_", " ").title()),
                "description": metadata.get("description", f"Execute the '{workflow_id}' workflow."),
                "available_inputs": available_inputs,
                "defaults": workflow_defaults,
                "updated_at": metadata.get("updated_at"),
                "hash": metadata.get("hash"),  # Could compute file hash if needed
            })
        
        return catalog
    
    def load_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Load workflow by ID with mtime-based cache invalidation.

        Checks file modification time on each call. If the file has been
        modified since last load, the cache entry is invalidated and the
        workflow is reloaded from disk.
        """
        workflow_path = self._safe_workflow_path(workflow_id)
        if not workflow_path:
            return None

        # Check if cached version is still fresh
        try:
            current_mtime = workflow_path.stat().st_mtime
        except OSError:
            current_mtime = None

        if workflow_id in self._workflow_cache:
            cached_mtime = self._workflow_mtime.get(workflow_id)
            if current_mtime is not None and cached_mtime == current_mtime:
                return copy.deepcopy(self._workflow_cache[workflow_id])
            else:
                logger.info("Workflow '%s' changed on disk (mtime %s -> %s), reloading", workflow_id, cached_mtime, current_mtime)

        try:
            with open(workflow_path, encoding="utf-8") as f:
                workflow = json.load(f)
            self._workflow_cache[workflow_id] = workflow
            if current_mtime is not None:
                self._workflow_mtime[workflow_id] = current_mtime
            return copy.deepcopy(workflow)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load workflow {workflow_id}: {e}")
            return None
    
    def apply_workflow_overrides(
        self,
        workflow: dict[str, Any],
        workflow_id: str,
        overrides: dict[str, Any],
        defaults_manager: DefaultsProvider | None = None,
    ) -> dict[str, Any]:
        """Apply constrained overrides to workflow based on metadata.

        The returned workflow dict contains an ``__override_report__`` key
        with 'overrides_applied' and 'overrides_dropped' dicts.  Callers
        should pop this key before submitting the workflow to ComfyUI.
        """

        workflow_path = self._safe_workflow_path(workflow_id)
        if not workflow_path:
            raise ValueError(f"Workflow {workflow_id} not found")

        metadata = self._load_workflow_metadata(workflow_path)
        override_mappings = metadata.get("override_mappings", {})
        constraints = metadata.get("constraints", {})

        # If no metadata, try to infer from PARAM_ placeholders
        if not override_mappings:
            parameters = self._extract_parameters(workflow)
            for param_name, param in parameters.items():
                if param_name not in override_mappings:
                    override_mappings[param_name] = param.bindings

        # Determine namespace for defaults
        namespace = self._determine_namespace(workflow_id)

        # Track which overrides were applied vs dropped
        overrides_applied = {}
        overrides_dropped = {}

        # Extract parameters once for type coercion
        parameters = self._extract_parameters(workflow)
        parameters = self._merge_metadata_parameters(parameters, metadata)

        # Apply overrides with constraints
        for param_name, value in overrides.items():
            if param_name not in override_mappings:
                logger.warning(f"Override '{param_name}' has no matching PARAM_ placeholder in {workflow_id}, skipping")
                overrides_dropped[param_name] = f"No matching PARAM_{param_name.upper()} placeholder in workflow"
                continue

            # Apply constraints if defined
            if param_name in constraints:
                constraint = constraints[param_name]
                if "enum" in constraint and value not in constraint["enum"]:
                    raise ValueError(f"Value '{value}' for '{param_name}' not in allowed enum: {constraint['enum']}")
                if "min" in constraint and value < constraint["min"]:
                    raise ValueError(f"Value '{value}' for '{param_name}' below minimum: {constraint['min']}")
                if "max" in constraint and value > constraint["max"]:
                    raise ValueError(f"Value '{value}' for '{param_name}' above maximum: {constraint['max']}")

            # Get parameter type from extracted parameters
            if param_name in parameters:
                param = parameters[param_name]
                coerced_value = self._coerce_value(value, param.annotation)
            else:
                coerced_value = self._coerce_override_from_metadata(param_name, value, metadata)

            # Apply to all bindings
            for node_id, input_name in override_mappings[param_name]:
                if node_id in workflow and "inputs" in workflow[node_id]:
                    workflow[node_id]["inputs"][input_name] = coerced_value
            overrides_applied[param_name] = value

        # Apply defaults for parameters not in overrides
        for param_name, param in parameters.items():
            if param_name not in overrides and not param.required:
                default_value = None
                if defaults_manager:
                    default_value = defaults_manager.get_default(namespace, param.name, None)
                if default_value is None:
                    default_value = self._get_parameter_default(param.name, param.annotation, metadata)
                if default_value is not None:
                    for node_id, input_name in param.bindings:
                        if node_id in workflow and "inputs" in workflow[node_id]:
                            workflow[node_id]["inputs"][input_name] = default_value

        # Store the report on the workflow dict so callers can access it
        # (using a private key that won't conflict with node IDs which are numeric strings)
        workflow["__override_report__"] = {
            "overrides_applied": overrides_applied,
            "overrides_dropped": overrides_dropped,
        }

        return workflow

    def _refresh_definition_if_stale(self, definition: WorkflowToolDefinition) -> None:
        """Reload a tool definition's template from disk if the file has been modified."""
        workflow_path = self._safe_workflow_path(definition.workflow_id)
        if not workflow_path:
            return

        try:
            current_mtime = workflow_path.stat().st_mtime
        except OSError:
            return

        cached_mtime = self._workflow_mtime.get(definition.workflow_id)
        if cached_mtime is not None and cached_mtime == current_mtime:
            return  # File hasn't changed

        logger.info("Refreshing tool definition '%s' from disk (mtime changed)", definition.workflow_id)
        try:
            with open(workflow_path, encoding="utf-8") as f:
                workflow = json.load(f)
            metadata = self._load_workflow_metadata(workflow_path)
            definition.template = workflow
            definition.parameters = self._merge_metadata_parameters(
                self._extract_parameters(workflow), metadata
            )
            definition.output_preferences = self._guess_output_preferences(workflow)
            self._workflow_cache[definition.workflow_id] = workflow
            self._workflow_mtime[definition.workflow_id] = current_mtime
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to refresh workflow %s: %s", definition.workflow_id, e)

    def _load_workflows(self):
        definitions: list[WorkflowToolDefinition] = []
        if not self.workflows_dir.exists():
            logger.info("Workflow directory %s does not exist yet", self.workflows_dir)
            return definitions

        for workflow_path in sorted(self.workflows_dir.rglob("*.json")):
            if workflow_path.name.endswith(".meta.json"):
                continue
            try:
                with open(workflow_path, encoding="utf-8") as handle:
                    workflow = json.load(handle)
            except json.JSONDecodeError as exc:
                logger.error("Skipping workflow %s due to JSON error: %s", workflow_path.name, exc)
                continue

            if not isinstance(workflow, dict):
                logger.error("Skipping workflow %s: root JSON must be an object", workflow_path.name)
                continue
            if WorkflowManager.is_ui_workflow_export(workflow):
                logger.info(
                    "Skipping workflow %s: UI/editor export (use API-format JSON for MCP)",
                    workflow_path.name,
                )
                continue

            parameters = self._extract_parameters(workflow)
            parameters = self._merge_metadata_parameters(parameters, self._load_workflow_metadata(workflow_path))
            if not parameters:
                logger.info(
                    "Workflow %s has no %s placeholders; skipping auto-tool registration",
                    workflow_path.name,
                    PLACEHOLDER_PREFIX,
                )
                continue

            rel = workflow_path.relative_to(self.workflows_dir)
            nested_id = str(rel.with_suffix("")).replace("\\", "/")
            tool_name = self._dedupe_tool_name(self._derive_tool_name(workflow_path.stem))
            definition = WorkflowToolDefinition(
                workflow_id=nested_id,
                tool_name=tool_name,
                description=self._derive_description(workflow_path.stem),
                template=workflow,
                parameters=parameters,
                output_preferences=self._guess_output_preferences(workflow),
            )
            try:
                self._workflow_mtime[nested_id] = workflow_path.stat().st_mtime
            except OSError:
                pass
            logger.info(
                "Prepared workflow tool '%s' from %s with params %s",
                tool_name,
                workflow_path.name,
                list(parameters.keys()),
            )
            definitions.append(definition)

        return definitions

    def render_workflow(
        self,
        definition: WorkflowToolDefinition,
        provided_params: dict[str, Any],
        defaults_manager: DefaultsProvider | None = None,
    ):

        # Check if the workflow file has changed on disk and refresh the template
        self._refresh_definition_if_stale(definition)

        workflow = copy.deepcopy(definition.template)
        workflow_path = self._safe_workflow_path(definition.workflow_id)
        metadata = self._load_workflow_metadata(workflow_path) if workflow_path else {}
        
        # Determine namespace (image, audio, or video)
        namespace = self._determine_namespace(definition.workflow_id)
        
        for param in definition.parameters.values():
            if param.required and param.name not in provided_params:
                raise ValueError(f"Missing required parameter '{param.name}'")
            
            # Use provided value, default, or generate (for seed)
            raw_value = provided_params.get(param.name)
            if raw_value is None:
                if defaults_manager:
                    # Use defaults manager to get value with proper precedence
                    raw_value = defaults_manager.get_default(namespace, param.name, None)
                    if raw_value is not None:
                        logger.debug(f"Using default value for {param.name}: {raw_value}")
                if raw_value is None:
                    raw_value = self._get_parameter_default(param.name, param.annotation, metadata)
                    if raw_value is not None:
                        logger.debug(f"Using builtin default value for {param.name}: {raw_value}")
                if raw_value is None:
                    if param.required:
                        raise ValueError(f"Missing required parameter '{param.name}'")
                    else:
                        continue
            
            coerced_value = self._coerce_value(raw_value, param.annotation)
            for node_id, input_name in param.bindings:
                workflow[node_id]["inputs"][input_name] = coerced_value
        
        return workflow

    def _extract_parameters(self, workflow: dict[str, Any]):
        parameters: OrderedDict[str, WorkflowParameter] = OrderedDict()
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            for input_name, value in inputs.items():
                parsed = self._parse_placeholder(value)
                if not parsed:
                    continue
                param_name, annotation, placeholder_value = parsed
                description = PLACEHOLDER_DESCRIPTIONS.get(
                    param_name, f"Value for '{param_name}'."
                )
                parameter = parameters.get(param_name)
                if not parameter:
                    # Make seed and other optional parameters non-required
                    # Only 'prompt' should be required for generate_image
                    # Only 'tags' and 'lyrics' should be required for generate_song
                    # Only 'prompt' should be required for generate_video
                    optional_params = {
                        "seed", "width", "height", "model", "steps", "cfg",
                        "sampler_name", "scheduler", "denoise", "negative_prompt",
                        "seconds", "lyrics_strength",  # Audio-specific optional params
                        "duration", "fps"  # Video-specific optional params
                    }
                    is_required = param_name not in optional_params
                    parameter = WorkflowParameter(
                        name=param_name,
                        placeholder=placeholder_value,
                        annotation=annotation,
                        description=description,
                        required=is_required,
                    )
                    parameters[param_name] = parameter
                parameter.bindings.append((node_id, input_name))
        return parameters

    def _parse_placeholder(self, value):
        if not isinstance(value, str) or not value.startswith(PLACEHOLDER_PREFIX):
            return None
        token = value[len(PLACEHOLDER_PREFIX) :]
        annotation = str
        if "_" in token:
            type_candidate, remainder = token.split("_", 1)
            type_hint = PLACEHOLDER_TYPE_HINTS.get(type_candidate.upper())
            if type_hint:
                annotation = type_hint
                token = remainder
        param_name = self._normalize_name(token)
        return param_name, annotation, value

    def _normalize_name(self, raw: str):
        cleaned = [
            (char.lower() if char.isalnum() else "_")
            for char in raw.strip()
        ]
        normalized = "".join(cleaned).strip("_")
        return normalized or "param"

    def _derive_tool_name(self, stem: str):
        return self._normalize_name(stem)

    def _dedupe_tool_name(self, base_name: str):
        name = base_name or "workflow_tool"
        if name not in self._tool_names:
            self._tool_names.add(name)
            return name
        suffix = 2
        while f"{name}_{suffix}" in self._tool_names:
            suffix += 1
        deduped = f"{name}_{suffix}"
        self._tool_names.add(deduped)
        return deduped

    def _derive_description(self, stem: str):
        readable = stem.replace("_", " ").replace("-", " ").strip()
        readable = readable if readable else stem
        return f"Execute the '{readable}' ComfyUI workflow."

    def _determine_namespace(self, workflow_id: str) -> str:
        """Determine namespace based on workflow ID."""
        tail = workflow_id.rsplit("/", 1)[-1]
        if tail == "generate_song":
            return "audio"
        if tail == "generate_video":
            return "video"
        return "image"  # default fallback
    
    def _guess_output_preferences(self, workflow: dict[str, Any]):
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).lower()
            if "audio" in class_type:
                return AUDIO_OUTPUT_KEYS
            if "video" in class_type or "savevideo" in class_type or "videocombine" in class_type:
                return VIDEO_OUTPUT_KEYS
        return DEFAULT_OUTPUT_KEYS

    def _coerce_override_from_metadata(self, param_name: str, value: Any, metadata: dict[str, Any]) -> Any:
        """When workflow JSON has no PARAM_ placeholders, use .wfmeta / .meta.json available_inputs types."""
        spec = (metadata.get("available_inputs") or {}).get(param_name) or {}
        t = spec.get("type", "str")
        if t == "int":
            return self._coerce_value(value, int)
        if t == "float":
            return self._coerce_value(value, float)
        if t == "bool":
            return self._coerce_value(value, bool)
        return self._coerce_value(value, str)

    def _coerce_value(self, value: Any, annotation: type):
        """Coerce a value to the specified type with proper error handling."""
        try:
            if annotation is str:
                return str(value)
            if annotation is int:
                return int(value)
            if annotation is float:
                return float(value)
            if annotation is bool:
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "y"}
                return bool(value)
            return value
        except (ValueError, TypeError) as e:
            raise ValueError(f"Cannot convert {value!r} to {annotation.__name__}: {e}")

    def _get_builtin_default(self, param_name: str, annotation: type) -> Any:
        if param_name == "seed" and annotation is int:
            return random.randint(0, 2**32 - 1)
        if param_name in OPTIONAL_PARAM_DEFAULTS:
            return self._coerce_value(OPTIONAL_PARAM_DEFAULTS[param_name], annotation)
        return None

    def _get_parameter_default(self, param_name: str, annotation: type, metadata: dict[str, Any]) -> Any:
        metadata_defaults = metadata.get("defaults", {})
        if param_name in metadata_defaults:
            return self._coerce_value(metadata_defaults[param_name], annotation)
        return self._get_builtin_default(param_name, annotation)

    def _infer_defaults(self, parameters: OrderedDict[str, WorkflowParameter]) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        for param in parameters.values():
            if param.required:
                continue
            default_value = self._get_builtin_default(param.name, param.annotation)
            if default_value is not None:
                defaults[param.name] = default_value
        return defaults

    def _annotation_from_type_name(self, raw_type: str) -> type:
        mapped = {
            "str": str,
            "string": str,
            "text": str,
            "int": int,
            "integer": int,
            "float": float,
            "number": float,
            "bool": bool,
            "boolean": bool,
        }
        return mapped.get((raw_type or "str").strip().lower(), str)

    def _merge_metadata_parameters(
        self,
        parameters: OrderedDict[str, WorkflowParameter],
        metadata: dict[str, Any],
    ) -> OrderedDict[str, WorkflowParameter]:
        available_inputs = metadata.get("available_inputs") or {}
        override_mappings = metadata.get("override_mappings") or {}
        if not available_inputs:
            return parameters

        merged = OrderedDict(parameters)
        for param_name, spec in available_inputs.items():
            if not isinstance(spec, dict):
                continue
            annotation = self._annotation_from_type_name(str(spec.get("type", "str")))
            description = spec.get(
                "description",
                PLACEHOLDER_DESCRIPTIONS.get(param_name, f"Value for '{param_name}'."),
            )
            required = bool(spec.get("required", False))
            bindings = [(str(node_id), str(input_name)) for node_id, input_name in override_mappings.get(param_name, [])]

            existing = merged.get(param_name)
            if existing is not None:
                existing.annotation = annotation
                existing.description = description
                existing.required = required
                if bindings:
                    existing.bindings = bindings
                continue

            merged[param_name] = WorkflowParameter(
                name=param_name,
                placeholder=f"metadata:{param_name}",
                annotation=annotation,
                description=description,
                required=required,
                bindings=bindings,
            )
        return merged
