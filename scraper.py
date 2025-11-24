"""
Apify-based YouTube Transcript Scraper for Hawaii Legislative Channels

Modes:
1) Cloud Run service (default): exposes HTTP endpoints:
   - GET /health
   - POST /run  (kicks off a scrape + BQ upload)
2) CLI/local: `python scraper.py` runs a scrape once and exits.

Scrapes completed live stream videos from Hawaii Senate and House YouTube channels,
extracts transcripts via Apify actor starvibe/youtube-video-transcript (English if available),
and uploads them to Google BigQuery with deduplication.

BigQuery schema expected (segment-level table):
  video_id STRING
  segment_index INTEGER
  start_sec INTEGER
  end_sec INTEGER
  text STRING
  created_at TIMESTAMP (default CURRENT_TIMESTAMP())
"""

import os
import sys
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import bigquery, secretmanager
from google.oauth2 import service_account
from apify_client import ApifyClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("scraper")

app = FastAPI(title="UH YouTube Transcript Scraper (Apify)", version="0.4.0")


# -------------------------------
# Env loading / config
# -------------------------------
def load_environment_variables() -> None:
    """Load environment variables from Secret Manager (Cloud Run) or .env (local)."""
    project_id = os.getenv("GCP_PROJECT_ID", "its-gro")
    secret_name = f"projects/{project_id}/secrets/youtube-scraper-env/versions/latest"

    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": secret_name})
        payload = response.payload.data.decode("utf-8")

        for line in payload.strip().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

        logger.info("✓ Loaded environment variables from Secret Manager")
        return

    except Exception as e:
        logger.info(f"Secret Manager load failed ({e}); falling back to .env")
        load_dotenv()
        logger.info("✓ Loaded environment variables from .env (if present)")


load_environment_variables()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
BQ_DATASET = os.getenv("BQ_DATASET", "")
BQ_TABLE = os.getenv("BQ_TABLE", "")
BQ_MENTIONS_TABLE = os.getenv("BQ_MENTIONS_TABLE", "uh_mentions_explicit")
EXPLICIT_KEYWORDS = [k.strip().lower() for k in os.getenv("EXPLICIT_KEYWORDS", "").split(",") if k.strip()]
SERVICE_ACCOUNT_KEY_PATH = os.getenv("SERVICE_ACCOUNT_KEY_PATH")  # optional local
MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "10"))

# Apify config
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "starvibe/youtube-video-transcript")
APIFY_LANGUAGE = os.getenv("APIFY_LANGUAGE", "en")

CHANNELS = [
    {"name": "Hawaii Senate", "channel_id": "UCekvvdL_uyq2DUyj1GjlrOA"},
    {"name": "Hawaii House of Representatives", "channel_id": "UCvoLAX1ww3e63K8qQ5of0bw"},
]


def validate_configuration() -> None:
    required = {
        "YOUTUBE_API_KEY": YOUTUBE_API_KEY,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "BQ_DATASET": BQ_DATASET,
        "BQ_TABLE": BQ_TABLE,
        "APIFY_TOKEN": APIFY_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        msg = "Missing required env vars: " + ", ".join(missing)
        logger.error(msg)
        raise RuntimeError(msg)


# -------------------------------
# Scraper core
# -------------------------------
class YouTubeTranscriptScraper:
    def __init__(self):
        validate_configuration()
        self.youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        self.bq_client = self._init_bigquery_client()
        self.table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
        self.mentions_table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"
        self.apify = ApifyClient(APIFY_TOKEN)

    def _init_bigquery_client(self) -> bigquery.Client:
        if SERVICE_ACCOUNT_KEY_PATH and os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_KEY_PATH,
                scopes=["https://www.googleapis.com/auth/bigquery"],
            )
            logger.info(f"Using service account key at {SERVICE_ACCOUNT_KEY_PATH}")
            return bigquery.Client(credentials=creds, project=GCP_PROJECT_ID)

        logger.info("Using default credentials")
        return bigquery.Client(project=GCP_PROJECT_ID)

    def get_existing_video_ids(self) -> Set[str]:
        query = f"SELECT DISTINCT video_id FROM `{self.table_id}`"
        try:
            job = self.bq_client.query(query)
            return {row.video_id for row in job.result()}
        except Exception as e:
            logger.warning(f"Could not fetch existing IDs (continuing anyway): {e}")
            return set()

    def get_channel_videos(self, channel_id: str, channel_name: str) -> List[str]:
        video_ids: List[str] = []

        try:
            logger.info(f"Fetching completed live streams from {channel_name}...")
            req = self.youtube.search().list(
                part="id",
                channelId=channel_id,
                eventType="completed",
                type="video",
                order="date",
                maxResults=min(MAX_VIDEOS_PER_CHANNEL, 50),
            )
            resp = req.execute()

            for item in resp.get("items", []):
                if item["id"]["kind"] == "youtube#video":
                    video_ids.append(item["id"]["videoId"])

            while "nextPageToken" in resp and len(video_ids) < MAX_VIDEOS_PER_CHANNEL:
                req = self.youtube.search().list(
                    part="id",
                    channelId=channel_id,
                    eventType="completed",
                    type="video",
                    order="date",
                    maxResults=min(MAX_VIDEOS_PER_CHANNEL - len(video_ids), 50),
                    pageToken=resp["nextPageToken"],
                )
                resp = req.execute()

                for item in resp.get("items", []):
                    if item["id"]["kind"] == "youtube#video":
                        video_ids.append(item["id"]["videoId"])

        except HttpError as e:
            logger.error(f"YouTube API error for {channel_name}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error for {channel_name}: {e}")

        logger.info(f"Found {len(video_ids)} videos for {channel_name}")
        return video_ids

    # -------- Apify transcript path --------
    def get_transcript_apify(self, video_id: str) -> List[Dict[str, Any]]:
        """
        Calls Apify actor for a single video and returns a list of segments:
        [{start: float, end: float, text: str}, ...]
        """
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        run_input = {
            "youtube_url": youtube_url,
            "language": APIFY_LANGUAGE,
        }

        last_err = None
        for attempt in range(1, 4):
            try:
                logger.info(f"Apify transcript fetch ({attempt}/3) for {video_id}")
                run = self.apify.actor(APIFY_ACTOR_ID).call(run_input=run_input)
                dataset_id = run.get("defaultDatasetId")
                if not dataset_id:
                    logger.warning(f"No dataset returned by Apify for {video_id}")
                    return []

                items = list(self.apify.dataset(dataset_id).iterate_items())
                if not items:
                    logger.warning(f"Empty Apify dataset for {video_id}")
                    return []

                item = items[0]
                transcript = item.get("transcript") or []

                segments = []
                for seg in transcript:
                    text = (seg.get("text") or "").strip()
                    if not text:
                        continue
                    start = float(seg.get("start", 0.0))
                    end = float(seg.get("end", start))
                    segments.append({"start": start, "end": end, "text": text})

                return segments

            except Exception as e:
                last_err = e
                logger.warning(f"Apify attempt {attempt} failed for {video_id}: {e}")
                time.sleep(2.0 * attempt)

        logger.error(f"Apify transcript failed for {video_id}: {last_err}")
        return []

    def upload_to_bigquery(self, video_id: str, segments: List[Dict[str, Any]]) -> tuple[bool, str, str]:
        if not segments:
            return (False, "", "")

        video_name = ""
        try:
            resp = self.youtube.videos().list(part="snippet", id=video_id).execute()
            items = resp.get("items", [])
            if items:
                video_name = items[0].get("snippet", {}).get("title", "") or ""
        except Exception as e:
            logger.debug(f"Could not fetch video title for {video_id}: {e}")

        hst = timezone(timedelta(hours=-10))
        now_timestamp = datetime.now(tz=hst).isoformat()

        rows = []
        for idx, seg in enumerate(segments):
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
            rows.append(
                {
                    "video_id": video_id,
                    "segment_index": idx,
                    "start_sec": int(start),
                    "end_sec": int(end),
                    "text": seg.get("text", ""),
                    "created_at": now_timestamp,
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "video_name": video_name,
                }
            )

        try:
            errors = self.bq_client.insert_rows_json(self.table_id, rows)
            if errors:
                logger.error(f"BQ insert errors for {video_id}: {errors}")
                return (False, video_name, now_timestamp)

            logger.info(f"Uploaded {len(rows)} segments for {video_id}")
            return (True, video_name, now_timestamp)

        except Exception as e:
            logger.error(f"BQ upload error for {video_id}: {e}")
            return (False, video_name, now_timestamp)

    def extract_explicit_mentions(
        self,
        video_id: str,
        segments: List[Dict[str, Any]],
        video_name: str,
        now_timestamp: str,
    ) -> List[Dict[str, Any]]:
        if not EXPLICIT_KEYWORDS:
            return []

        rows: List[Dict[str, Any]] = []
        for idx, seg in enumerate(segments):
            text = seg.get("text", "") or ""
            lt = text.lower()
            for kw in EXPLICIT_KEYWORDS:
                if kw in lt:
                    start = int(float(seg.get("start", 0)))
                    end = int(float(seg.get("end", start)))
                    rows.append(
                        {
                            "video_id": video_id,
                            "segment_index": idx,
                            "start_sec": start,
                            "end_sec": end,
                            "text": text,
                            "video_url": f"https://www.youtube.com/watch?v={video_id}",
                            "video_name": video_name,
                            "keyword": kw,
                            "created_at": now_timestamp,
                        }
                    )
                    break
        return rows

    def upload_mentions(self, rows: List[Dict[str, Any]]) -> bool:
        if not rows:
            return True
        vid = rows[0]["video_id"]
        try:
            errors = self.bq_client.insert_rows_json(self.mentions_table_id, rows)
            if errors:
                logger.error(f"BQ mention insert errors for {vid}: {errors}")
                return False
            logger.info(f"Uploaded {len(rows)} explicit mention segments for {vid}")
            return True
        except Exception as e:
            logger.error(f"BQ mention upload error for {vid}: {e}")
            return False

    def process_video(self, video_id: str) -> bool:
        segments = self.get_transcript_apify(video_id)
        if not segments:
            logger.info(f"No transcript for {video_id}; skipping")
            return False

        ok_full, video_name, now_ts = self.upload_to_bigquery(video_id, segments)
        mention_rows = self.extract_explicit_mentions(video_id, segments, video_name, now_ts)
        ok_mentions = self.upload_mentions(mention_rows)
        return ok_full and ok_mentions

    def run_once(self) -> Dict[str, int]:
        existing = self.get_existing_video_ids()

        processed = skipped = failed = 0

        for ch in CHANNELS:
            vids = self.get_channel_videos(ch["channel_id"], ch["name"])
            new_vids = [v for v in vids if v not in existing]
            skipped += len(vids) - len(new_vids)

            for v in new_vids:
                logger.info(f"Processing new video: {v}")
                ok = self.process_video(v)
                if ok:
                    processed += 1
                else:
                    failed += 1
                time.sleep(1.5)

        return {"processed": processed, "skipped": skipped, "failed": failed}


# -------------------------------
# Service wrapper state
# -------------------------------
_last_summary: Optional[Dict[str, int]] = None
_is_running = False
_lock = threading.Lock()


def _run_scrape_job():
    global _last_summary, _is_running

    with _lock:
        if _is_running:
            logger.info("Scrape already running; refusing to start another.")
            return
        _is_running = True

    try:
        scraper = YouTubeTranscriptScraper()
        summary = scraper.run_once()
        _last_summary = summary
        logger.info(f"Scrape summary: {summary}")

    except Exception as e:
        logger.error(f"Scrape fatal error: {e}", exc_info=True)
        _last_summary = {"processed": 0, "skipped": 0, "failed": 0}

    finally:
        with _lock:
            _is_running = False


@app.get("/health")
def health():
    return {"ok": True, "running": _is_running, "last_summary": _last_summary}


@app.post("/run")
def run():
    _run_scrape_job()
    return {"status": "completed", "summary": _last_summary}


# -------------------------------
# CLI entrypoint
# -------------------------------
def main():
    try:
        scraper = YouTubeTranscriptScraper()
        summary = scraper.run_once()
        logger.info(f"Done. Summary: {summary}")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

from fastapi import Query
from google.cloud import bigquery as _bq

@app.get("/search")
def search_explicit_mentions(
    q: str = Query(..., description="keyword/text to search for"),
    limit: int = Query(50, ge=1, le=500),
):
    client = _bq.Client(project=GCP_PROJECT_ID)
    table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"

    sql = f"""
    SELECT video_id, segment_index, start_sec, end_sec, text, video_url, video_name, keyword, created_at
    FROM `{table}`
    WHERE LOWER(text) LIKE @pat OR LOWER(keyword) LIKE @kw
    ORDER BY created_at DESC
    LIMIT @limit
    """

    job_config = _bq.QueryJobConfig(
        query_parameters=[
            _bq.ScalarQueryParameter("pat", "STRING", f"%{q.lower()}%"),
            _bq.ScalarQueryParameter("kw", "STRING", q.lower()),
            _bq.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()

    out = []
    for r in rows:
        ts_url = f"{r.video_url}&t={int(r.start_sec)}s"
        out.append({
            "video_id": r.video_id,
            "segment_index": r.segment_index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "text": r.text,
            "keyword": r.keyword,
            "video_name": r.video_name,
            "timestamp_url": ts_url,
            "created_at": str(r.created_at),
        })
    return {"q": q, "count": len(out), "results": out}

from fastapi.responses import HTMLResponse

@app.get("/ui", response_class=HTMLResponse)
def ui():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>UH Explicit Mentions Search</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; }
    input { width: 360px; padding: 8px; }
    button { padding: 8px 12px; margin-left: 6px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin: 10px 0; }
    .meta { color: #666; font-size: 0.9em; margin-bottom: 6px; }
    .text { white-space: pre-wrap; }
  </style>
</head>
<body>
  <h2>UH Explicit Mentions</h2>
  <p>Search explicit mention segments. Click timestamp to open YouTube.</p>

  <input id="q" placeholder="e.g., board of regents, mauna kea, cancer center" />
  <button onclick="runSearch()">Search</button>
  <span id="status"></span>

  <div id="results"></div>

<script>
async function runSearch(){
  const q = document.getElementById("q").value.trim();
  if(!q){ return; }
  document.getElementById("status").textContent = " searching...";
  const res = await fetch(`/search?q=${encodeURIComponent(q)}&limit=50`);
  const data = await res.json();
  document.getElementById("status").textContent = ` ${data.count} results`;
  const root = document.getElementById("results");
  root.innerHTML = "";
  for(const r of data.results){
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <div class="meta"><b>${r.video_name || r.video_id}</b></div>
      <div class="meta">keyword: <code>${r.keyword}</code> • start: ${r.start_sec}s</div>
      <div class="text">${r.text}</div>
      <div><a href="${r.timestamp_url}" target="_blank">Open at timestamp</a></div>
    `;
    root.appendChild(div);
  }
}
</script>
</body>
</html>
"""
