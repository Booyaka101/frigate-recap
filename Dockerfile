FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --system --uid 1000 recap \
    && mkdir -p /recaps \
    && chown recap:recap /recaps
USER recap
WORKDIR /recaps

LABEL org.opencontainers.image.title="frigate-recap" \
      org.opencontainers.image.description="Turn one day of Frigate NVR events into a single digest MP4" \
      org.opencontainers.image.source="https://github.com/Booyaka101/frigate-recap" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["frigate-recap"]
CMD ["--help"]
