# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Env hygiene
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy requirements first for caching
COPY requirements.txt .

# Install deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY scraper.py .

# Cloud Run listens on 8080
EXPOSE 8080

# Start FastAPI server
CMD ["uvicorn", "scraper:app", "--host", "0.0.0.0", "--port", "8080"]
