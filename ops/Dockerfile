FROM python:3.11-slim-bookworm AS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.30
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Keep the frozen dependency environment shared across source-only releases.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

FROM dependencies AS runtime
COPY src ./src
RUN chmod -R a+rX /app/src
RUN uv sync --frozen --no-dev --no-cache
RUN useradd --uid 10001 --create-home fri
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG FRI_RELEASE_SHA
LABEL org.opencontainers.image.revision=$FRI_RELEASE_SHA
USER 10001:10001
RUN python -c 'from discord.opus import Encoder, Decoder; pcm = bytes(3840); assert len(Decoder().decode(Encoder().encode(pcm, 960))) == len(pcm)'
CMD ["fri"]
