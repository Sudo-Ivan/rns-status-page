# Note: If chainguard images stop working, replace with Alpine variants. 

FROM cgr.dev/chainguard/python:latest-dev AS builder

WORKDIR /app

RUN python -m venv venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rns_status_page ./rns_status_page/
COPY setup.py .

FROM cgr.dev/chainguard/python:latest

WORKDIR /app

COPY --from=builder /app/venv /app/venv
COPY --from=builder /app/rns_status_page ./rns_status_page/
COPY --from=builder /app/setup.py .

COPY --chown=65532:65532 config /home/nonroot/.reticulum/config

ENV PATH="/app/venv/bin:$PATH"

EXPOSE 5000

LABEL org.opencontainers.image.source="https://github.com/Sudo-Ivan/rns-status-page"
LABEL org.opencontainers.image.description="Reticulum network status page."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="Sudo-Ivan"

ENTRYPOINT ["python", "rns_status_page/status_page.py"]
