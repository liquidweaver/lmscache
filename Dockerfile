FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY lmscache ./lmscache
RUN pip install --no-cache-dir .
ENV LMSCACHE_MODELS=/models LMSCACHE_CONFIG=/config HF_HOME=/config/hf-home HOME=/config
EXPOSE 8080
CMD ["uvicorn", "lmscache.app:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "3"]
