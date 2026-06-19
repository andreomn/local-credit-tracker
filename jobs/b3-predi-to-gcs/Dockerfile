FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY b3-predi-job.py .

CMD ["python", "-u", "b3-predi-job.py"]