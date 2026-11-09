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

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/api/health')"

CMD ["python", "app.py"]
