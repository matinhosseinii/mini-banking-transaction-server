FROM postgres:17

ARG PATRONI_VERSION=4.0.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-psycopg2 \
    && python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        "patroni[kubernetes]==${PATRONI_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

USER postgres

ENTRYPOINT ["patroni"]
CMD ["/etc/patroni/patroni.yml"]