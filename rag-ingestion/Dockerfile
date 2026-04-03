FROM python:3.11-slim

WORKDIR /app

COPY rag-ingestion/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY rag-ingestion/ /app/

CMD ["python", "/app/ingest.py"]
