FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CTF_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY ctf_platform ./ctf_platform
COPY gunicorn.conf.py ./

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "ctf_platform:create_app()"]
