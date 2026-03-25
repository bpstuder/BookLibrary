FROM python:3.12-slim

# System deps for Pillow + curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev libwebp-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime directories (overridden by Docker volumes in production)
RUN mkdir -p data/covers library

EXPOSE 8000

# --forwarded-allow-ips=* : trust X-Forwarded-* headers from any upstream proxy
#                           (Nginx, Traefik, Cloudflare tunnel, etc.)
# --proxy-headers         : parse X-Forwarded-For / X-Forwarded-Proto
# --no-server-header      : don't expose uvicorn version in responses
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--forwarded-allow-ips", "*", \
     "--proxy-headers", \
     "--no-server-header"]
