FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEEKDB_DATA=/data

WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir . \
    && groupadd --system seekdb \
    && useradd --system --gid seekdb --home-dir /data --shell /usr/sbin/nologin seekdb \
    && mkdir -p /data \
    && chown -R seekdb:seekdb /data

USER seekdb
VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["seekdb"]
CMD ["serve"]
