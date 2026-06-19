FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY anbima_to_gcs.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "anbima_to_gcs.py"]