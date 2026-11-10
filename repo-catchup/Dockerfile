FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir "mistralai-workflows[mistralai]>=3.10"

COPY src/ /app/src/
ENV PYTHONPATH=/app/src

CMD ["python", "-m", "worker.entrypoints.worker"]
