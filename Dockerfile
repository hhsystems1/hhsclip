# HHS Clips — Docker image
# Python server + clipper (faster-whisper + yt-dlp + ffmpeg).
# Same image runs on the Mac mini, a VPS, or anywhere Docker runs.
FROM python:3.12-slim

# ffmpeg (clipper cuts + probes with it) and a C compiler toolchain
# for any wheels that need building.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data dirs the app writes to (mounted as volumes in compose).
RUN mkdir -p input output jobs review

EXPOSE 8000

# entrypoint restores accounts + session secret from /state on start
# and saves them back on stop, so sign-in survives container rebuilds.
ENTRYPOINT ["/app/entrypoint.sh"]
