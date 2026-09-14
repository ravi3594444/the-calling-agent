FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# `static/` is copied BEFORE the install, not after it: the browser client
# ships inside the wheel (see [tool.setuptools] in pyproject.toml), so it has
# to be present in the directory pip builds from. Copied afterwards it was
# simply absent from the installed package, and since nothing here sets
# STATIC_DIR the container answered /healthz 200 while every page load was a
# 500. The copy at /app/static is left in place so STATIC_DIR=/app/static
# still names a real directory, but nothing needs it now.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY static/ ./static/
RUN pip install --no-cache-dir .

# Run unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "calling_agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
