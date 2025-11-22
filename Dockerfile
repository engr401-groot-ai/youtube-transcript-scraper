# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application codebase
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run FastAPI on Cloud Run port
# This assumes your FastAPI instance is named `app` inside scraper.py
CMD ["uvicorn", "scraper:app", "--host", "0.0.0.0", "--port", "8080"]
