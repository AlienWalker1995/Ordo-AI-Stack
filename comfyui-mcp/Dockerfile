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

# Server binds to 0.0.0.0:9000; ComfyUI at comfyui:8188
ENV COMFYUI_URL=http://comfyui:8188
EXPOSE 9000

# Use stdio so Docker MCP Gateway can communicate (it spawns containers and uses stdin/stdout)
CMD ["python", "server.py", "--stdio"]
