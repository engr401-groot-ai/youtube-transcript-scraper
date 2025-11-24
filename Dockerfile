FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY scraper.py main.py ./

ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
