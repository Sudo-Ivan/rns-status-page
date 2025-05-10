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

ENTRYPOINT ["python", "rns_status_page/status_page.py"]
