FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy
ENV PYTHONUNBUFFERED=1 USE_PATCHRIGHT=0 MODE=http
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "daznbet_odds.py"]