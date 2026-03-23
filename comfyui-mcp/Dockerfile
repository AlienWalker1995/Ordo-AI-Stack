# ComfyUI MCP Server — joenorton/comfyui-mcp-server
# Connects to remote ComfyUI for image/audio/video generation.
FROM python:3.12-slim

WORKDIR /app

# Clone and install from source (includes workflows, COMFYUI_URL support)
RUN apt-get update -qq && apt-get install -y -qq git curl && rm -rf /var/lib/apt/lists/* \
  && git clone --depth 1 https://github.com/joenorton/comfyui-mcp-server.git . \
  && pip install --no-cache-dir -r requirements.txt \
  && sed -i 's/port=9000,/port=9000, host="0.0.0.0",/' server.py

# AI-toolkit: accept flat run_workflow args + optional default workflow_id (see tools/workflow.py)
COPY tools/workflow.py tools/workflow.py
# AI-toolkit: catalog + overrides when workflow JSON uses literals (ComfyUI UI) instead of PARAM_ placeholders
COPY managers/workflow_manager.py managers/workflow_manager.py
# AI-toolkit: MCP tools for custom-node pip + comfyui restart (ops-controller)
COPY tools/management.py tools/management.py
RUN python3 <<'PY'
from pathlib import Path
p = Path("server.py")
t = p.read_text(encoding="utf-8")
if "register_management_tools" in t:
    raise SystemExit(0)
needle_import = "from tools.workflow import register_workflow_tools"
if needle_import not in t:
    raise SystemExit("server.py: expected import anchor not found")
t = t.replace(
    needle_import,
    needle_import + "\nfrom tools.management import register_management_tools",
    1,
)
needle_reg = "register_workflow_tools(mcp, workflow_manager, comfyui_client, defaults_manager, asset_registry)"
if needle_reg not in t:
    raise SystemExit("server.py: expected register_workflow_tools call not found")
t = t.replace(
    needle_reg,
    needle_reg + "\nregister_management_tools(mcp)",
    1,
)
p.write_text(t, encoding="utf-8")
PY

# Server binds to 0.0.0.0:9000; ComfyUI at comfyui:8188
ENV COMFYUI_URL=http://comfyui:8188
EXPOSE 9000

# Use stdio so Docker MCP Gateway can communicate (it spawns containers and uses stdin/stdout)
CMD ["python", "server.py", "--stdio"]
