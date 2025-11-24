FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py ./
COPY index.html ./
COPY video.html ./

ENV PORT=8080
EXPOSE 8080

# Run Uvicorn directly as the container and allows proper signal handling.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
