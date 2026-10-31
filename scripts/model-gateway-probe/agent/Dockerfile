# Build context is scripts/model-gateway-probe so probe.py is shared with the mock VM:
#   docker build --platform linux/amd64 -f scripts/model-gateway-probe/agent/Dockerfile scripts/model-gateway-probe
FROM python:3.12-slim
ENV PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
WORKDIR /app/user_agent
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY probe.py agent/main.py ./
EXPOSE 8088
CMD ["python", "main.py"]
