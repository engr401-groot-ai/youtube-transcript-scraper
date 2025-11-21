# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scraper.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the scraper
CMD ["python", "scraper.py"]
