# ComfyUI MCP Server — joenorton/comfyui-mcp-server
# Connects to remote ComfyUI for image/audio/video generation.
FROM python:3.12-slim

WORKDIR /app

# Clone and install from source (includes workflows, COMFYUI_URL support)
RUN apt-get update -qq && apt-get install -y -qq git curl && rm -rf /var/lib/apt/lists/* \
  && git clone --depth 1 https://github.com/joenorton/comfyui-mcp-server.git . \
  && pip install --no-cache-dir -r requirements.txt \
  && sed -i 's/mcp\.run(transport="streamable-http")/mcp.run(transport="streamable-http", host="0.0.0.0")/' server.py

# Server binds to 0.0.0.0:9000; ComfyUI at comfyui:8188
ENV COMFYUI_URL=http://comfyui:8188
EXPOSE 9000

CMD ["python", "server.py"]
