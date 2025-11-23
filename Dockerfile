FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy the whole repo (small anyway)
COPY . .

ENV PORT=8080

# point uvicorn at scraper.py's FastAPI app
CMD ["uvicorn", "scraper:app", "--host", "0.0.0.0", "--port", "8080"]
