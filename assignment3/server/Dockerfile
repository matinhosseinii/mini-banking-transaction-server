FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Use Runflare Debian mirrors
RUN sed -i \
    -e 's|http://deb.debian.org/debian-security|http://mirror-linux.runflare.com/debian-security|g' \
    -e 's|http://deb.debian.org/debian|http://mirror-linux.runflare.com/debian|g' \
    /etc/apt/sources.list.d/debian.sources
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY server/requirements.txt /tmp/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=app:app server/app /app/server/app
COPY --chown=app:app server/alembic.ini /app/server/alembic.ini
COPY --chown=app:app server/migrations /app/server/migrations
COPY --chown=app:app server/shard_map.json /app/server/shard_map.json

USER app

EXPOSE 8000

CMD ["python", "-m", "server.app.run"]
