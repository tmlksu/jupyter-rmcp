FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole package (server.py + config/state/outputs/util/app/reaper +
# the tool modules + the backends/ subpackage). A .dockerignore keeps caches out.
# NOTE: adding a new module needs no Dockerfile change now — but a module missing
# at runtime means the container silently runs stale code, so keep this a full copy.
COPY . .

# non-root
RUN useradd -m -u 1001 app
USER app

EXPOSE 8000
CMD ["python", "server.py"]
