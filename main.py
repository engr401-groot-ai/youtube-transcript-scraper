"""
Apify-based YouTube Transcript Scraper for Hawaii Legislative Channels

Key components and behavior
    - YouTubeTranscriptScraper: core class that calls the YouTube API to
        list completed live streams, calls an Apify actor to extract transcripts,
    - FastAPI endpoints:
            GET /health      - basic liveness + last summary
            POST /run       - trigger an async scrape job
            GET /search     - query explicit mentions (reads BigQuery)
            GET /ui         - serves a local `index.html` UI
            GET /          - redirects to /ui
            GET /favicon.ico- (handled; returns 204)

Usage(s)
    - Cloud Run service exposes HTTP endpoints
    - Dev server: `uvicorn main:app --reload --host 127.0.0.1 --port 8000`
    - CLI run: `python main.py`
    - Container: Dockerfile runs `uvicorn main:app ...` so the image serves the API/UI.
"""

import os
import sys
import logging
import time
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
import html as html_escape
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import bigquery, secretmanager
from google.cloud import run_v2
import google.auth
from apify_client import ApifyClient

# -------------------------------
# Env loading / configs
# -------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("scraper")

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="UH YouTube Transcript Scraper (Apify)", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://youtube-transcript-scraper.lovable.app",  # published Lovable site
        "https://lovable.dev",
    ],
    allow_origin_regex=r"https://.*\.lovableproject\.com",  # preview subdomains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



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
BQ_HEARING_VIDEOS_TABLE = os.getenv("BQ_HEARING_VIDEOS_TABLE", "")
BQ_MENTIONS_TABLE = os.getenv("BQ_MENTIONS_TABLE", "")
EXPLICIT_SHEET_ID = os.getenv("EXPLICIT_SHEET_ID", "")
EXPLICIT_SHEET_TAB = os.getenv("EXPLICIT_SHEET_TAB", "")
EXPLICIT_SHEET_RANGE = os.getenv("EXPLICIT_SHEET_RANGE", "")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "starvibe/youtube-video-transcript")
APIFY_LANGUAGE = os.getenv("APIFY_LANGUAGE", "en")
CLOUD_RUN_REGION = os.getenv("CLOUD_RUN_REGION", "us-central1")

MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "10"))

def _load_explicit_keywords_from_sheet(sheet_id: str, value_range: str = "A:A") -> List[str]:
    """Load keywords from a Google Sheet (first column). Returns list of lowercase keywords.
    Uses Application Default Credentials. Expects the sheet to have a header
    (e.g., 'Term') — the header will be skipped.
    """
    if not sheet_id:
        return []

    # Use ADC to call Sheets API once and return results (no retries, no fallback)
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        logger.info("Using application default credentials for Sheets API (explicit keywords)")
        service = build("sheets", "v4", credentials=creds)
        resp = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=value_range).execute()
        values = resp.get("values", []) or []

        # Log that the sheet range was successfully opened and how many rows were returned
        try:
            logger.info(f"Opened sheet {sheet_id} range {value_range} — {len(values)} rows")
        except Exception:
            # Avoid any unexpected logging errors from breaking keyword loading
            logger.info(f"Opened sheet {sheet_id} range {value_range}")

        kws: List[str] = []
        for i, row in enumerate(values):
            if not row:
                continue
            cell = str(row[0]).strip()
            # Skip header-like first row
            if i == 0 and cell.lower() in ("term", "keyword", "keywords"):
                continue
            if cell:
                kws.append(cell.lower())

        # de-duplicate while preserving order
        seen = set()
        out: List[str] = []
        for k in kws:
            if k and k not in seen:
                seen.add(k)
                out.append(k)

        logger.info(f"Loaded {len(out)} explicit keywords from sheet {sheet_id}")
        return out

    except Exception as e:
        logger.warning(f"Could not load explicit keywords from sheet {sheet_id}: {e}")
        return []

# Load explicit keywords from Google Sheets 
# If EXPLICIT_SHEET_ID is not set, EXPLICIT_KEYWORDS will be an empty list and no explicit mentions will be extracted
if EXPLICIT_SHEET_ID:
    value_range = EXPLICIT_SHEET_RANGE
    if EXPLICIT_SHEET_TAB:
        value_range = f"{EXPLICIT_SHEET_TAB}!{EXPLICIT_SHEET_RANGE}"

    EXPLICIT_KEYWORDS = _load_explicit_keywords_from_sheet(EXPLICIT_SHEET_ID, value_range)
    if not EXPLICIT_KEYWORDS:
        logger.warning("Explicit keywords sheet configured but returned no keywords; explicit mention extraction disabled.")
        EXPLICIT_KEYWORDS = []
else:
    logger.info("No EXPLICIT_SHEET_ID configured — explicit mention extraction disabled.")
    EXPLICIT_KEYWORDS = []

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "starvibe/youtube-video-transcript")
APIFY_LANGUAGE = os.getenv("APIFY_LANGUAGE", "en")

MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "10"))

CHANNELS = [
    {"name": "Hawaii Senate", "channel_id": "UCekvvdL_uyq2DUyj1GjlrOA"},
    {"name": "Hawaii House of Representatives", "channel_id": "UCvoLAX1ww3e63K8qQ5of0bw"},
]

def validate_configuration() -> None:
    required = {
        "YOUTUBE_API_KEY": YOUTUBE_API_KEY,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "BQ_DATASET": BQ_DATASET,
        "BQ_HEARING_VIDEOS_TABLE": BQ_HEARING_VIDEOS_TABLE,
        "APIFY_TOKEN": APIFY_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        msg = "Missing required env vars: " + ", ".join(missing)
        logger.error(msg)
        raise RuntimeError(msg)

# -------------------------------
# Youtube core
# -------------------------------
class YouTubeTranscriptScraper:
    def __init__(self):
        validate_configuration()
        self.youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        self.bq_client = self._init_bigquery_client()
        self.table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_HEARING_VIDEOS_TABLE}"
        self.mentions_table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"
        self.apify = ApifyClient(APIFY_TOKEN)

    def _init_bigquery_client(self) -> bigquery.Client:
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])
            logger.info("Using application default credentials for BigQuery")
            return bigquery.Client(credentials=creds, project=GCP_PROJECT_ID)
        except Exception:
            logger.info("Falling back to BigQuery client default; ADC will be used if available")
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

    # -------- Apify transcript extraction & upload --------
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

    # -------- Extract & upload explicit mentions --------
    def extract_explicit_mentions(
        self,
        video_id: str,
        segments: List[Dict[str, Any]],
        video_name: str,
        now_timestamp: str,
    ) -> List[Dict[str, Any]]:
        if not EXPLICIT_SHEET_ID:
            return []

        value_range = EXPLICIT_SHEET_RANGE
        if EXPLICIT_SHEET_TAB:
            value_range = f"{EXPLICIT_SHEET_TAB}!{EXPLICIT_SHEET_RANGE}"

        keywords = _load_explicit_keywords_from_sheet(EXPLICIT_SHEET_ID, value_range)
        if not keywords:
            return []

        rows: List[Dict[str, Any]] = []

        # Use regex whole-word matching to avoid substring false positives.
        # For each keyword build a pattern like r"\bkeyword\b" (escaped).
        patterns = [(kw, re.compile(r"\b" + re.escape(kw) + r"\b", flags=re.IGNORECASE)) for kw in keywords if kw]

        for idx, seg in enumerate(segments):
            text = seg.get("text", "") or ""
            for kw, pat in patterns:
                if pat.search(text):
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

    # -------- Proccess video & run scraper --------
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

def trigger_notification_job():
    """
    Trigger the Cloud Run Job named `notification-system-job` in `CLOUD_RUN_REGION`.
    This uses the Cloud Run Admin client (run_v2). The function logs errors
    but does not raise so it won't crash the scraper on notification failures.
    """
    if not GCP_PROJECT_ID:
        logger.warning("GCP_PROJECT_ID not configured; cannot trigger notifier job")
        return

    location = CLOUD_RUN_REGION
    job_name = f"projects/{GCP_PROJECT_ID}/locations/{location}/jobs/notification-system-job"

    try:
        client = run_v2.JobsClient()
        request = run_v2.RunJobRequest(name=job_name)
        client.run_job(request=request)
        logger.info(f"Successfully triggered notification job: {job_name}")
    except Exception as e:
        logger.error(f"Failed to trigger notification job {job_name}: {e}")
        return

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

        try:
            if isinstance(summary, dict) and summary.get("processed", 0) > 0:
                logger.info("New videos found! Triggering the notification job...")
                trigger_notification_job()
            else:
                logger.info("No new videos. Skipping notification job trigger.")
        except Exception as e:
            logger.warning(f"Error while attempting to trigger notification job: {e}")

    except Exception as e:
        logger.error(f"Scrape fatal error: {e}", exc_info=True)
        _last_summary = {"processed": 0, "skipped": 0, "failed": 0}

    finally:
        with _lock:
            _is_running = False

# -------------------------------
# Fast API endpoints
# -------------------------------
@app.get("/health")
def health():
    return {"ok": True, "running": _is_running, "last_summary": _last_summary}

@app.post("/run")
def run():
    _run_scrape_job()
    return {"status": "completed", "summary": _last_summary}

@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root to the UI page"""
    return RedirectResponse(url="/ui")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Return 204 for favicon requests (no favicon)."""
    return Response(status_code=204)

@app.get("/ui")
def ui():
    """Serve the static/index.html file if present, otherwise return a simple 404 HTML response."""
    index_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)

@app.get("/video/{video_id}/view", include_in_schema=False)
def video_page(video_id: str):
    """Render a simple dynamic page for a given video_id.
    """

    client = bigquery.Client(project=GCP_PROJECT_ID)
    hv_table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.hearing_videos"

    sql = f"""
    SELECT video_name, video_url
    FROM `{hv_table}`
    WHERE video_id = @vid
    LIMIT 1
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("vid", "STRING", video_id),
        ]
    )

    rows = list(client.query(sql, job_config=job_config).result())
    video_name = None
    video_url = None
    if rows:
        r = rows[0]
        video_name = getattr(r, "video_name", None)
        video_url = getattr(r, "video_url", None)

    title = video_name or video_id

    tpl_path = os.path.join(os.path.dirname(__file__), "video.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        tpl = f.read()

    safe_title = html_escape.escape(title)
    safe_video_id = html_escape.escape(video_id)
    safe_video_url = html_escape.escape(video_url or "")

    content = tpl.replace("%%TITLE%%", safe_title).replace("%%VIDEO_ID%%", safe_video_id).replace("%%VIDEO_URL%%", safe_video_url)

    return HTMLResponse(content=content, status_code=200)

@app.get("/search")
def search_explicit_mentions(
    q: str = Query(..., description="keyword/text to search for"),
    limit: int = Query(50, ge=1, le=500),
):
    client = bigquery.Client(project=GCP_PROJECT_ID)
    table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"

    sql = f"""
    SELECT video_id, segment_index, start_sec, end_sec, text, video_url, video_name, keyword, created_at
    FROM `{table}`
    WHERE LOWER(text) LIKE @pat OR LOWER(keyword) LIKE @kw
    ORDER BY created_at DESC
    LIMIT @limit
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("pat", "STRING", f"%{q.lower()}%"),
            bigquery.ScalarQueryParameter("kw", "STRING", q.lower()),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
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

# TODO: MAKE A NEW BIGQUERY TABLE FOR VIDEOS (video_id, video_url, video_name) AND UPDATE THE SCRAPER TO UPLOAD TO THAT TABLE
@app.get("/list-videos")
def list_videos(
    limit: int = Query(100, ge=1, le=5000, description="maximum number of videos to return"),
):
    """List unique videos from the hearing_videos table.

    Returns distinct video_id with their video_url and video_name. Default limit is 100.
    """
    client = bigquery.Client(project=GCP_PROJECT_ID)
    table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.hearing_videos"

    sql = f"""
    WITH vids AS (
        SELECT DISTINCT video_id, video_url, video_name
        FROM `{table}`
        WHERE video_id IS NOT NULL AND video_id != ''
    )
    SELECT v.video_id, v.video_url, v.video_name, COALESCE(m.cnt, 0) AS mention_count
    FROM vids v
    LEFT JOIN (
        SELECT video_id, COUNT(1) AS cnt
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}`
        WHERE video_id IS NOT NULL AND video_id != ''
        GROUP BY video_id
    ) m
    ON v.video_id = m.video_id
    ORDER BY v.video_name ASC
    LIMIT @limit
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()

    out = []
    for r in rows:
        out.append({
            "video_id": r.video_id,
            "video_url": getattr(r, "video_url", None),
            "video_name": getattr(r, "video_name", None),
            "mention_count": int(getattr(r, "mention_count", 0) or 0),
        })

    return {"count": len(out), "results": out}


@app.get("/video/{video_id}")
def get_video_detail(video_id: str):
    """Return full transcript segments and mentions for a specific video_id.

    Response JSON: {"video_id": str, "video_name": str, "video_url": str, "transcript": [...], "mentions": [...]}
    """
    client = bigquery.Client(project=GCP_PROJECT_ID)

    # Fetch transcript segments from main table
    table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_HEARING_VIDEOS_TABLE}"
    sql_tx = f"""
    SELECT segment_index, start_sec, end_sec, text, video_url, video_name, created_at
    FROM `{table}`
    WHERE video_id = @vid
    ORDER BY segment_index ASC
    """

    job_config_tx = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("vid", "STRING", video_id),
        ]
    )

    tx_rows = list(client.query(sql_tx, job_config=job_config_tx).result())

    transcript = []
    video_name = None
    video_url = None
    for r in tx_rows:
        transcript.append({
            "segment_index": r.segment_index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "text": r.text,
            "created_at": str(r.created_at),
        })
        if not video_name:
            video_name = getattr(r, "video_name", None)
        if not video_url:
            video_url = getattr(r, "video_url", None)

    # Fetch mentions for this video
    mentions_table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"
    sql_mn = f"""
    SELECT segment_index, start_sec, end_sec, text, keyword, video_url, video_name, created_at
    FROM `{mentions_table}`
    WHERE video_id = @vid
    ORDER BY start_sec ASC
    """

    job_config_mn = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("vid", "STRING", video_id),
        ]
    )

    mn_rows = list(client.query(sql_mn, job_config=job_config_mn).result())

    mentions = []
    for r in mn_rows:
        mentions.append({
            "segment_index": r.segment_index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "text": r.text,
            "keyword": getattr(r, "keyword", None),
            "created_at": str(r.created_at),
        })

    return {
        "video_id": video_id,
        "video_name": video_name,
        "video_url": video_url,
        "transcript": transcript,
        "mentions": mentions,
    }

@app.get("/list_mentions")
def list_mentions(
    limit: int = Query(100, ge=1, le=1000, description="maximum number of mentions to return"),
):
    """List recent explicit mentions from the mentions table, newest first.

    Returns rows with video_id, segment_index, start_sec, end_sec, text, video_url,
    video_name, keyword, created_at.
    """
    client = bigquery.Client(project=GCP_PROJECT_ID)
    table = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"

    sql = f"""
    SELECT video_id, segment_index, start_sec, end_sec, text, video_url, video_name, keyword, created_at
    FROM `{table}`
    WHERE video_id IS NOT NULL
    ORDER BY created_at DESC
    LIMIT @limit
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()

    out = []
    for r in rows:
        ts_url = None
        try:
            ts_url = f"{r.video_url}&t={int(r.start_sec)}s"
        except Exception:
            ts_url = getattr(r, "video_url", None)

        out.append({
            "video_id": r.video_id,
            "segment_index": r.segment_index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "text": r.text,
            "keyword": getattr(r, "keyword", None),
            "video_name": getattr(r, "video_name", None),
            "video_url": getattr(r, "video_url", None),
            "timestamp_url": ts_url,
            "created_at": str(r.created_at),
        })

    return {"count": len(out), "results": out}

@app.get("/api/mentions/recent")
def get_recent_mentions(
    hours: int = Query(24, ge=1, description="How many hours back to fetch"),
):
    """
    Returns mentions created in the last X hours.
    """

    client = bigquery.Client(project=GCP_PROJECT_ID)
    table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"

    sql = f"""
    SELECT video_name, keyword, text, video_url, start_sec, created_at
    FROM `{table_id}`
    WHERE CAST(created_at AS TIMESTAMP) > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
    ORDER BY created_at DESC
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("hours", "INT64", hours),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()

    results = []
    for r in rows:
        try:
            link = f"{r.video_url}&t={int(r.start_sec)}s"
        except Exception:
            link = getattr(r, "video_url", None)

        results.append({
            "video_name": getattr(r, "video_name", None),
            "keyword": getattr(r, "keyword", None),
            "text": getattr(r, "text", None),
            "video_url": getattr(r, "video_url", None),
            "link": link,
            "start_sec": getattr(r, "start_sec", None),
            "created_at": str(getattr(r, "created_at", None)),
        })

    return {"count": len(results), "results": results}

@app.get("/keywords")
def keywords():
    """Return explicit keywords from the configured Google Sheet.
    It returns a JSON array of lowercased keywords (deduplicated, order-preserving).
    """
    if not EXPLICIT_SHEET_ID:
        return {"count": 0, "keywords": []}

    value_range = EXPLICIT_SHEET_RANGE
    if EXPLICIT_SHEET_TAB:
        value_range = f"{EXPLICIT_SHEET_TAB}!{EXPLICIT_SHEET_RANGE}"

    kws = _load_explicit_keywords_from_sheet(EXPLICIT_SHEET_ID, value_range)
    return {"count": len(kws), "keywords": kws}

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
