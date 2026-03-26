FROM python:3.12-slim

# System deps:
#   libjpeg-dev / zlib1g-dev / libwebp-dev — Pillow image support
#   curl                                   — healthcheck
#   gosu                                   — privilege drop in entrypoint
#   passwd / shadow-utils (via login)      — usermod / groupmod
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev libwebp-dev \
    curl gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create a non-root user/group that will be remapped at runtime via PUID/PGID.
# UID/GID 1000 are just defaults — entrypoint.sh overrides them.
RUN groupadd -g 1000 appgroup \
 && useradd  -u 1000 -g appgroup -s /bin/sh -M appuser \
 && mkdir -p data/covers library \
 && chown -R appuser:appgroup /app

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

# --forwarded-allow-ips=* : trust X-Forwarded-* headers from any upstream proxy
# --proxy-headers         : parse X-Forwarded-For / X-Forwarded-Proto
# --no-server-header      : don't expose uvicorn version in responses
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--forwarded-allow-ips", "*", \
     "--proxy-headers", \
     "--no-server-header"]
