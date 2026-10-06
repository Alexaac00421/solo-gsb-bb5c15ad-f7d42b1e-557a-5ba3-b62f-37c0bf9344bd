FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WATER_HOST=0.0.0.0 \
    WATER_PORT=8080 \
    WATER_DB_PATH=/data/water.db

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app
COPY examples ./examples

# 数据库目录（可由 compose 挂载卷覆盖）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 先初始化数据库表（幂等），再启动 API
CMD ["sh", "-c", "python -c 'from app.database import init_db; init_db()' && uvicorn app.main:app --host ${WATER_HOST} --port ${WATER_PORT}"]
