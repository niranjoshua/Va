# One image for the API, the dashboard and the daily pipeline.
#
#   docker build -t vaticore .                                          # API, dashboard, pipeline
#   docker build -t vaticore-fm --build-arg VATICORE_WITH_FOUNDATION=1 .  # plus Chronos-2, baked in
#   docker run -p 8000:8000 vaticore
#
# Render passes a service's environment variables as build arguments, so
# setting VATICORE_WITH_FOUNDATION=1 on a service builds it with the models.
FROM python:3.11-slim-trixie

ARG VATICORE_WITH_FOUNDATION=0
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG WITH_PG_CLIENT=1
ARG UV_VERSION=0.8.17

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/app/.hf \
    UV_NO_SYNC=1 \
    VATICORE_WITH_FOUNDATION=${VATICORE_WITH_FOUNDATION}

# System libraries: libgomp for LightGBM (the slim base image lacks it), and the
# Postgres client tools for backups and restore checks (pg_dump, pg_restore),
# from the PostgreSQL project's repository because they must be at least as
# new as the server and Debian's own are older.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && if [ "$WITH_PG_CLIENT" = "1" ]; then \
         apt-get install -y --no-install-recommends ca-certificates curl gnupg \
         && install -d /usr/share/postgresql-common/pgdg \
         && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
              https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
              > /etc/apt/sources.list.d/pgdg.list \
         && apt-get update \
         && apt-get install -y --no-install-recommends postgresql-client-17 \
         && apt-get purge -y gnupg && apt-get autoremove -y; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged user the services run as.
RUN useradd --create-home --uid 10001 vaticore

# uv for reproducible installs from the committed lockfile (from PyPI).
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

# Dependency layer: copy only what the install needs, so it caches across code
# changes. hatchling needs the package sources and README to build the wheel.
COPY pyproject.toml uv.lock README.md ./
COPY vaticore ./vaticore
RUN uv sync --frozen --no-dev --extra service --extra postgres --extra dashboard --extra ops

# Foundation models (Chronos-2) with CPU-only PyTorch and the weights baked in,
# so plans never wait on a download. Adds about 1 GB.
COPY docker/install_foundation.sh /tmp/install_foundation.sh
RUN if [ "$VATICORE_WITH_FOUNDATION" = "1" ]; then \
      sh /tmp/install_foundation.sh "$TORCH_INDEX" && chown -R vaticore /app/.hf; \
    fi \
    && rm /tmp/install_foundation.sh
# With the weights in the image, never reach out to Hugging Face at run time.
ENV HF_HUB_OFFLINE=${VATICORE_WITH_FOUNDATION}

# Example portfolio and recipients template, for the pipeline demo. Real
# portfolios and recipient lists are mounted as secret files, never baked in.
COPY examples/sites ./examples/sites

# Run unprivileged. The installed packages stay root-owned and read-only; the
# app directory itself is writable for a local DuckDB file.
RUN chown vaticore /app
USER vaticore

EXPOSE 8000

# Honour the platform provided PORT if set (Render, Railway, Fly), default 8000.
CMD ["sh", "-c", "uv run uvicorn vaticore.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
