FROM python:3.12-slim
WORKDIR /app
RUN useradd -m -u 1000 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
USER appuser
EXPOSE 9000
CMD ["python", "server.py"]
