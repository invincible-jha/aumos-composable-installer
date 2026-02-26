# Stage 1: Build
FROM python:3.11-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --prefix=/install --no-warn-script-location .

# Stage 2: Runtime
FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd -r aumos && useradd -r -g aumos -d /app -s /sbin/nologin aumos
COPY --from=builder /install /usr/local
COPY src/ /app/src/
WORKDIR /app
RUN chown -R aumos:aumos /app
USER aumos
ENTRYPOINT ["aumos"]
CMD ["--help"]
