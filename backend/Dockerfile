# NovelOS control platform. Legacy modules remain available for explicit migration.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOVELOS_DATA_DIR=/data

WORKDIR /app

# 先装依赖，改代码时不用重装
COPY requirements.txt requirements-vector.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-vector.txt

COPY app ./app
COPY seed ./seed
COPY platform_migrations ./platform_migrations

# /data 放 SQLite 库与正文镜像，由 compose 挂出来
RUN groupadd --gid 10001 novelos && useradd --uid 10001 --gid novelos --no-create-home novelos \
    && mkdir -p /data && chown novelos:novelos /data
VOLUME ["/data"]
USER novelos

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.platform.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
