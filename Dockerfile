FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
ENV PYTHONPATH=/app/src COPILOT_DB=/data/crm.db
VOLUME /data
RUN mkdir -p /data && python -m copilot.cli seed
CMD ["python", "-m", "copilot.mcp_server"]
