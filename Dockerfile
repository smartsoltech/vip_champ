FROM python:3.13-slim-trixie

ARG APP_REVISION=development
LABEL org.opencontainers.image.revision="${APP_REVISION}"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/usr/lib/postgresql/16/bin:${PATH}" \
    APP_REVISION="${APP_REVISION}"

WORKDIR /app
COPY scripts/install_pg_client.sh /tmp/install_pg_client.sh
RUN sh /tmp/install_pg_client.sh \
    && rm /tmp/install_pg_client.sh \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home app \
    && install -d -o 10001 -g 10001 -m 0700 /app/db /app/db/backups
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --chown=10001:10001 *.py /app/
COPY --chown=10001:10001 scripts/ /app/scripts/
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "main.py"]
