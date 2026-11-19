FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY gateway/requirements.txt /tmp/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY gateway /app/gateway
COPY server/app/__init__.py /app/server/app/__init__.py
COPY server/app/logging_config.py /app/server/app/logging_config.py
COPY server/app/sharding.py /app/server/app/sharding.py
COPY server/shard_map.json /app/server/shard_map.json

USER 65534

EXPOSE 8000

CMD ["python", "-m", "gateway.run"]
