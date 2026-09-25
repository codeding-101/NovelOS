# 后端镜像：FastAPI + SQLite。数据目录挂卷，别把库和正文塞进镜像层。
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

# /data 放 SQLite 库与正文镜像，由 compose 挂出来
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
