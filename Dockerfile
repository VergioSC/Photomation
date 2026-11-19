# Photomation container image.
#
# The app reads photos from a mounted folder, so run it with volumes:
#   docker build -t photomation .
#   docker run --rm -p 5000:5000 \
#       -v "$PWD/input:/data/input" \
#       -v "$PWD/output:/data/output" \
#       photomation
#
FROM python:3.14-slim

# No apt layer needed: requirements.txt uses opencv-python-headless, which does
# not link against libGL.

WORKDIR /app

# Dependencies first, so edits to the source don't invalidate the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Paths point at mounted volumes; the server must listen on all interfaces for
# published ports to reach it.
ENV PHOTOMATION_INPUT=/data/input \
    PHOTOMATION_OUTPUT=/data/output \
    PHOTOMATION_LUTS=/data/luts \
    PHOTOMATION_CACHE=/data/.cache \
    PHOTOMATION_HOST=0.0.0.0 \
    PHOTOMATION_PORT=5000

# A published port means other machines can reach this. app.py refuses to
# start on a non-loopback host without a password, so a deployed run needs:
#   -e PHOTOMATION_PASSWORD=something
# To read a shoot from S3 instead of a mounted folder, add boto3 to the image
# and set PHOTOMATION_STORAGE=s3 and PHOTOMATION_S3_BUCKET.

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/api/health')"

# Threads, not processes: an export streams progress for as long as it runs and
# holds its worker the whole time, so a second worker thread has to be free for
# the thumbnails the same page is still asking for. --timeout 0 because that
# stream is deliberately long-lived and gunicorn would otherwise kill it.
CMD ["sh", "-c", "gunicorn --bind ${PHOTOMATION_HOST}:${PHOTOMATION_PORT} --workers 1 --threads 8 --timeout 0 app:app"]
