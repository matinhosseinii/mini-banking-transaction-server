FROM python:3.12-slim
# Use Runflare Debian mirrors and install PostgreSQL client library
RUN sed -i \
    -e 's|http://deb.debian.org/debian-security|http://mirror-linux.runflare.com/debian-security|g' \
    -e 's|http://deb.debian.org/debian|http://mirror-linux.runflare.com/debian|g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY coordinator/requirements.txt /tmp/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY coordinator /app/coordinator
COPY server/app/__init__.py /app/server/app/__init__.py
COPY server/app/sharding.py /app/server/app/sharding.py
COPY server/shard_map.json /app/server/shard_map.json
USER 65534
EXPOSE 8000
CMD ["sh", "-c", "alembic -c /app/coordinator/alembic.ini upgrade head && python -m coordinator.run"]
