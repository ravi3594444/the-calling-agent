FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the install layer.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY static/ ./static/

# Run unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "calling_agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
