FROM python:3.11-slim-bookworm
RUN pip install --no-cache-dir uv==0.11.30
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --uid 10001 --create-home fri
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG FRI_RELEASE_SHA
LABEL org.opencontainers.image.revision=$FRI_RELEASE_SHA
USER 10001:10001
CMD ["fri"]
