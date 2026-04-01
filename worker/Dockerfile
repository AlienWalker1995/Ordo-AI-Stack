FROM python:3.11-slim

WORKDIR /app

# Install worker + dashboard dependencies
COPY dashboard/requirements.txt /tmp/dashboard-requirements.txt
COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/dashboard-requirements.txt -r /tmp/worker-requirements.txt

# Copy shared dashboard package and worker
COPY dashboard/ /app/dashboard/
COPY worker/worker.py /app/worker.py

CMD ["python", "/app/worker.py"]
