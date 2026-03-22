FROM python:3.12-slim

WORKDIR /app

RUN useradd -m -u 1000 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app.py imports dashboard.* — copy package tree and expose it on PYTHONPATH
COPY __init__.py settings.py routes_hub.py services_catalog.py dependency_registry.py app.py ./dashboard/
COPY dependency_registry.json ./dashboard/
COPY static ./dashboard/static/

ENV PYTHONPATH=/app

EXPOSE 8080
USER appuser
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
