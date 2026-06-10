# Tailscale Key Expiry Monitor — stdlib-only Python, no pip installs.
# Image ~55MB, idle RSS ~12-18MB.
FROM python:3.12-alpine

# Run unprivileged.
RUN addgroup -S monitor && adduser -S monitor -G monitor \
    && mkdir -p /data && chown monitor:monitor /data

WORKDIR /app
COPY monitor.py .

USER monitor

# Unbuffered logs so `docker logs` streams in real time.
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]

ENTRYPOINT ["python", "/app/monitor.py"]
