FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements-runtime.lock ./
RUN python -m pip wheel --wheel-dir /wheels -r requirements-runtime.lock
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    SB_VAULT_PATH=/data/vault
WORKDIR /app
COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels second-brain \
    && rm -rf /wheels \
    && groupadd --gid 1000 brain \
    && useradd --uid 1000 --gid brain --no-create-home brain \
    && mkdir -p /data/vault /data/jobs \
    && chown -R brain:brain /data
COPY examples ./examples
USER brain
EXPOSE 8000
ENTRYPOINT ["second-brain"]
CMD ["serve", "--transport", "streamable-http", "--host", "0.0.0.0"]

FROM runtime AS test
USER root
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
USER brain
ENTRYPOINT ["python"]
CMD ["-m", "pytest", "-q", "-p", "no:cacheprovider"]
