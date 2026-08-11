FROM python:3.12-slim
WORKDIR /app

# OpenCV's shared-library dependencies. requirements.txt deliberately pins
# the *headless* OpenCV build precisely so these wouldn't be needed -- but
# mediapipe declares a hard dependency on `opencv-contrib-python` (the GUI
# build), so pip installs both, and the GUI one ends up providing the `cv2`
# module. That build links against libGL/libglib/libxcb, none of which ship
# in python:3.12-slim, so `import cv2` -- and therefore `import ai_engine`,
# and therefore the whole app -- dies at startup with
# `ImportError: libxcb.so.1: cannot open shared object file`.
#
# This only bites in the container: the Windows wheels used for local
# development bundle their own dependencies, so a manual `uvicorn` run
# never sees it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Shell form so ${PORT} expands at runtime. Render (and most PaaS) assign a
# port through $PORT and expect the process to bind it; a hardcoded 8000
# gets the service marked unhealthy and cycled forever. Defaults to 8000 so
# docker-compose.yml -- which maps 8001:8000 and sets no PORT -- is
# unaffected.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
