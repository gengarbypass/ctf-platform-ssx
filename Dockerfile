ARG CTF_PYTHON_IMAGE=python:3.13-slim-bookworm
FROM ${CTF_PYTHON_IMAGE}

ARG CTF_PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CTF_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check --index-url "$CTF_PIP_INDEX_URL" -r requirements.txt \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY ctf_platform ./ctf_platform
COPY gunicorn.conf.py ./

ARG CTF_BUILD_REVISION=""
ENV CTF_BUILD_REVISION=${CTF_BUILD_REVISION}
LABEL org.opencontainers.image.revision=${CTF_BUILD_REVISION}

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "ctf_platform:create_app()"]
