# python:3.12-slim
FROM python@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

WORKDIR /app

RUN useradd -m -u 1000 appuser

# Install worker + dashboard dependencies (repo-root build context — services/ reorg)
COPY services/v1-parity/dashboard/requirements.txt /tmp/dashboard-requirements.txt
COPY services/worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/dashboard-requirements.txt -r /tmp/worker-requirements.txt

# Copy shared dashboard package and worker (TARGET /app/dashboard/ unchanged so
# `from dashboard.X import Y` still resolves in-image)
COPY services/v1-parity/dashboard/ /app/dashboard/
COPY services/worker/worker.py /app/worker.py

USER appuser
CMD ["python", "/app/worker.py"]
