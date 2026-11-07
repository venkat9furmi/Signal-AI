# Commentary QC - one image, two services (recorder and commentator page).
#
# ffmpeg lives in the image on purpose: it is the capture layer, and the most
# common way this system has broken on Windows was a shell that could not find
# it. In here, it cannot go missing.
FROM python:3.12-slim

# tzdata so log timestamps read as local time rather than UTC; ffmpeg for the
# HLS pull and decode.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Berlin \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing the code does not reinstall scipy every build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY models/ ./models/

# Measurements, claims and logs live on a volume: rebuilding the image must
# never touch a week of recorded audio measurements.
VOLUME ["/data"]

# Each service overrides this in compose; running the image bare prints the
# recorder's options rather than silently doing nothing.
CMD ["python", "-m", "commentary_qc.watch", "--help"]
