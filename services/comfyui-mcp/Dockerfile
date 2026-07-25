# ComfyUI MCP Server — joenorton/comfyui-mcp-server
# Connects to remote ComfyUI for image/audio/video generation.
FROM python:3.12-slim

WORKDIR /app

# Clone and install from source (includes workflows, COMFYUI_URL support)
# Pin to known-good commit for reproducible builds
RUN apt-get update -qq && apt-get install -y -qq git curl && rm -rf /var/lib/apt/lists/* \
  && git clone https://github.com/joenorton/comfyui-mcp-server.git . \
  && git checkout e0101b2312f3 \
  && pip install --no-cache-dir -r requirements.txt \
  && sed -i 's/port=9000,/port=9000, host="0.0.0.0",/' server.py

# Ordo AI Stack: fix stdio transport — redirect print() to stderr so stdout
# stays clean for JSON-RPC.  The upstream server.py uses print() for banners
# which corrupts the MCP gateway's stdio channel ("invalid character '='" error).
RUN python3 <<'PY'
from pathlib import Path
s = Path("server.py")
t = s.read_text()
# Insert a stderr redirect right after the imports, before any print() call.
# When --stdio is on the command line, rebind print to write to stderr.
anchor = 'logger = logging.getLogger("MCP_Server")'
patch = """\nimport functools as _ft
if "--stdio" in sys.argv:
    import builtins as _bi
    _bi.print = _ft.partial(_bi.print, file=sys.stderr)
"""
if "_bi.print" not in t:
    t = t.replace(anchor, anchor + patch, 1)
    s.write_text(t)
PY

# Ordo AI Stack: remove ALL upstream bundled workflows — nothing is baked here. Skills/agents
# compose their own graph and submit it via queue_prompt(workflow_json=...). In particular the
# song workflow lives in Hermes' private data
# (data/hermes/skills/creative/song-generation/generate_song.workflow.json) so it can be edited
# without an image rebuild. (generate_image.json also hardcoded a nonexistent checkpoint.)
RUN rm -f workflows/generate_image.json workflows/basic_api_test.json workflows/generate_song.json

# Ordo AI Stack: accept flat run_workflow args + optional default workflow_id (see tools/workflow.py)
COPY tools/workflow.py tools/workflow.py
# Ordo AI Stack: catalog + overrides when workflow JSON uses literals (ComfyUI UI) instead of PARAM_ placeholders
COPY managers/workflow_manager.py managers/workflow_manager.py
# Ordo AI Stack: MCP tools for custom-node pip + comfyui restart (ops-controller)
COPY tools/management.py tools/management.py
# Ordo AI Stack: MCP tools for ComfyUI system state (GPU, queue, models, nodes, extensions)
COPY tools/system.py tools/system.py
RUN python3 <<'PY'
from pathlib import Path
p = Path("server.py")
t = p.read_text(encoding="utf-8")
if "register_system_tools" in t:
    raise SystemExit(0)
needle_import = "from tools.workflow import register_workflow_tools"
if needle_import not in t:
    raise SystemExit("server.py: expected import anchor not found")
t = t.replace(
    needle_import,
    needle_import
    + "\nfrom tools.management import register_management_tools"
    + "\nfrom tools.system import register_system_tools",
    1,
)
needle_reg = "register_workflow_tools(mcp, workflow_manager, comfyui_client, defaults_manager, asset_registry)"
if needle_reg not in t:
    raise SystemExit("server.py: expected register_workflow_tools call not found")
t = t.replace(
    needle_reg,
    needle_reg
    + "\nregister_management_tools(mcp)"
    + "\nregister_system_tools(mcp)",
    1,
)
p.write_text(t, encoding="utf-8")
PY

# Server binds to 0.0.0.0:9000; ComfyUI at comfyui:8188
ENV COMFYUI_URL=http://comfyui:8188
EXPOSE 9000

# Use stdio so Docker MCP Gateway can communicate (it spawns containers and uses stdin/stdout)
CMD ["python", "server.py", "--stdio"]
