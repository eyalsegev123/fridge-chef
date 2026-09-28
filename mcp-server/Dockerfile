FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server_simple.py .
COPY http_server.py .
COPY tools_definitions.py .
COPY middleware/ ./middleware/
COPY config.json .

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    LOG_LEVEL=info

EXPOSE 8080

CMD ["python", "http_server.py"]
