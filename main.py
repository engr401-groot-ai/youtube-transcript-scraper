import os
import sys
import logging
import time
import re
import threading
import html as html_escape
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import bigquery, firestore
from apify_client import ApifyClient


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("scraper")

# Initialize FastAPI and CORS
app = FastAPI(title="Gro Office House and Senate YouTube Transcript Scraper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://youtube-transcript-scraper.lovable.app",  # published Lovable site
        "https://lovable.dev",
        "https://daily-youtube-scraper-655654578945.us-central1.run.app",
    ],
    allow_origin_regex=r"https://.*\.lovableproject\.com",  # preview subdomains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load environment variables
load_dotenv()

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
BQ_DATASET = os.getenv("BQ_DATASET", "")
BQ_HEARING_VIDEOS_TABLE = os.getenv("BQ_HEARING_VIDEOS_TABLE", "")
BQ_MENTIONS_TABLE = os.getenv("BQ_MENTIONS_TABLE", "")
EXPLICIT_SHEET_ID = os.getenv("EXPLICIT_SHEET_ID", "")
EXPLICIT_SHEET_TAB = os.getenv("EXPLICIT_SHEET_TAB", "")
EXPLICIT_SHEET_RANGE = os.getenv("EXPLICIT_SHEET_RANGE", "")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "starvibe/youtube-video-transcript")
APIFY_LANGUAGE = os.getenv("APIFY_LANGUAGE", "en")

MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "10"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))

def validate_configuration() -> None:
    """Validate that all required environment variables are set."""
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

def _load_explicit_keywords_from_sheet(sheet_id: str, value_range: str = "A:A") -> List[str]:
    """Load keywords from a Google Sheet (first column)."""
    if not sheet_id:
        return []

    # Use Application Default Credentials (ADC) to call the Sheets API.
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        logger.info("Using application default credentials for Sheets API (explicit keywords)")
        service = build("sheets", "v4", credentials=creds)
        resp = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=value_range).execute()
        values = resp.get("values", []) or []

        try:
            logger.info(f"Opened sheet {sheet_id} range {value_range} — {len(values)} rows")
        except Exception:
            logger.info(f"Opened sheet {sheet_id} range {value_range}")

        kws: List[str] = []
        for i, row in enumerate(values):
            if not row:
                continue
            cell = str(row[0]).strip()
            if i == 0 and cell.lower() in ("term", "keyword", "keywords"):
                continue
            if cell:
                kws.append(cell.lower())

        # De-duplicate while preserving order
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

# Load explicit keywords from Google Sheets (if configured)
# If no sheet is configured, `EXPLICIT_KEYWORDS` will be an empty list and
# explicit mention extraction will be disabled.
if EXPLICIT_SHEET_ID:
    value_range = EXPLICIT_SHEET_RANGE
    if EXPLICIT_SHEET_TAB:
        value_range = f"{EXPLICIT_SHEET_TAB}!{EXPLICIT_SHEET_RANGE}"

    EXPLICIT_KEYWORDS = _load_explicit_keywords_from_sheet(EXPLICIT_SHEET_ID, value_range)
    if not EXPLICIT_KEYWORDS:
        logger.warning(
            "Explicit keywords sheet configured but returned no keywords; explicit mention extraction disabled."
        )
        EXPLICIT_KEYWORDS = []
else:
    logger.info("No EXPLICIT_SHEET_ID configured — explicit mention extraction disabled.")
    EXPLICIT_KEYWORDS = []

# YouTube channels and their channel IDs (monitored channels)
CHANNELS = [
    {"name": "Hawaii Senate", "channel_id": "UCekvvdL_uyq2DUyj1GjlrOA"},
    {"name": "Hawaii House of Representatives", "channel_id": "UCvoLAX1ww3e63K8qQ5of0bw"},
]

# Core YouTubeTranscriptScraper class
class YouTubeTranscriptScraper:
    def __init__(self):
        """Initialize scraper clients and configuration.

        Validates required environment variables, initializes the YouTube API
        client, BigQuery client, table identifiers, and the Apify client.
        """
        validate_configuration()
        self.youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)
        self.bq_client = self._init_bigquery_client()
        self.table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_HEARING_VIDEOS_TABLE}"
        self.mentions_table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MENTIONS_TABLE}"
        self.apify = ApifyClient(APIFY_TOKEN)

    def _init_bigquery_client(self) -> bigquery.Client:
        """Create and return a BigQuery client.

        Attempts to use Application Default Credentials (ADC); if that fails,
        falls back to the default BigQuery client constructor.
        """
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])
            logger.info("Using application default credentials for BigQuery")
            return bigquery.Client(credentials=creds, project=GCP_PROJECT_ID)
        except Exception:
            logger.info("Falling back to BigQuery client default; ADC will be used if available")
            return bigquery.Client(project=GCP_PROJECT_ID)

    def get_existing_video_ids(self) -> Set[str]:
        """Return a set of distinct `video_id` values already in the table.

        On error, logs a warning and returns an empty set so the caller can
        continue processing new videos.
        """
        query = f"SELECT DISTINCT video_id FROM `{self.table_id}`"
        try:
            job = self.bq_client.query(query)
            return {row.video_id for row in job.result()}
        except Exception as e:
            logger.warning(f"Could not fetch existing IDs (continuing anyway): {e}")
            return set()

    def get_channel_videos(self, channel_id: str, channel_name: str) -> List[str]:
        """Return recent completed video IDs for the given channel.

        Steps:
        1. Resolve the channel's uploads playlist ID.
        2. List recent items from the uploads playlist.
        3. Fetch video metadata and filter out livestreams and items
           older than `LOOKBACK_DAYS`.
        Returns up to `MAX_VIDEOS_PER_CHANNEL` most recent IDs.
        """
        try:
            logger.info(f"Fetching videos from {channel_name} via Uploads playlist...")
            
            # Step 1: Resolve the uploads playlist ID for the channel
            ch_resp = self.youtube.channels().list(
                id=channel_id,
                part="contentDetails"
            ).execute()
            
            items = ch_resp.get("items", [])
            if not items:
                logger.warning(f"Channel {channel_name} ({channel_id}) not found")
                return []
                
            uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            
            # Step 2: Fetch recent items from the uploads playlist (max 50)
            pl_req = self.youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=50
            )
            pl_resp = pl_req.execute()
            
            pl_items = pl_resp.get("items", [])
            if not pl_items:
                logger.info(f"No videos found in uploads playlist for {channel_name}")
                return []
                
            video_ids = [item["contentDetails"]["videoId"] for item in pl_items]
            
            # Step 3: Fetch video details to filter out livestreams and old uploads
            vid_req = self.youtube.videos().list(
                part="snippet,liveStreamingDetails",
                id=",".join(video_ids)
            )
            vid_resp = vid_req.execute()
            
            valid_videos = []
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
            
            for item in vid_resp.get("items", []):
                vid = item["id"]
                snippet = item["snippet"]
                live_details = item.get("liveStreamingDetails", {})
                
                # Check broadcast status
                broadcast_content = snippet.get("liveBroadcastContent", "none")
                if broadcast_content in ("upcoming", "live"):
                    logger.debug(f"Skipping {broadcast_content} video: {vid}")
                    continue
                    
                # Determine date: prefer actualEndTime, then actualStartTime, then publishedAt
                date_str = live_details.get("actualEndTime") or live_details.get("actualStartTime") or snippet["publishedAt"]
                
                try:
                    if date_str.endswith("Z"):
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(date_str)
                except ValueError:
                    dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                
                if dt >= cutoff_date:
                    valid_videos.append({"id": vid, "date": dt})
                else:
                    pass

            # Sort by date descending
            valid_videos.sort(key=lambda x: x["date"], reverse=True)
            
            final_ids = [v["id"] for v in valid_videos[:MAX_VIDEOS_PER_CHANNEL]]
            logger.info(f"Found {len(final_ids)} completed recent videos (last {LOOKBACK_DAYS} days) for {channel_name}")
            return final_ids

        except HttpError as e:
            logger.error(f"YouTube API error for {channel_name}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error for {channel_name}: {e}")
            return []

    def get_transcript_apify(self, video_id: str) -> List[Dict[str, Any]]:
        """Call Apify actor for `video_id` and return transcript segments.

        Returns a list of segment dictionaries: [{"start": float, "end": float, "text": str}, ...].
        The method retries up to several times before returning an empty list on failure.
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
        """Upload transcript segments for `video_id` into BigQuery.

        Returns a tuple: (success: bool, video_name: str, created_timestamp: str).
        """
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

        # Use Hawaii Standard Time for the `created_at` timestamp
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
        """Scan transcript segments for configured explicit keywords.

        Uses whole-word, case-insensitive regex matching to avoid false positives.
        Returns a list of rows suitable for inserting into the mentions table.
        """
        if not EXPLICIT_SHEET_ID:
            return []

        value_range = EXPLICIT_SHEET_RANGE
        if EXPLICIT_SHEET_TAB:
            value_range = f"{EXPLICIT_SHEET_TAB}!{EXPLICIT_SHEET_RANGE}"

        keywords = _load_explicit_keywords_from_sheet(EXPLICIT_SHEET_ID, value_range)
        if not keywords:
            return []

        rows: List[Dict[str, Any]] = []

        # Build whole-word, case-insensitive regex patterns for each keyword
        patterns = [
            (kw, re.compile(r"\b" + re.escape(kw) + r"\b", flags=re.IGNORECASE)) for kw in keywords if kw
        ]

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
        """Insert explicit mention rows into the mentions BigQuery table.

        Returns True on success. If `rows` is empty returns True immediately.
        Logs and returns False on failure.
        """
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
        """Process a single video: fetch transcript, upload segments, and
        extract and upload explicit mentions.

        Returns True only if both transcript upload and mention upload succeed.
        """
        segments = self.get_transcript_apify(video_id)
        if not segments:
            logger.info(f"No transcript for {video_id}; skipping")
            return False

        ok_full, video_name, now_ts = self.upload_to_bigquery(video_id, segments)
        mention_rows = self.extract_explicit_mentions(video_id, segments, video_name, now_ts)
        ok_mentions = self.upload_mentions(mention_rows)
        return ok_full and ok_mentions

    def run_once(self) -> Dict[str, int]:
        """Run one scraping cycle across all configured channels.

        Returns a summary dict with counts for processed, skipped, and failed.
        """
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

# Service wrapper state (keeps track of last run summary and concurrency)
_last_summary: Optional[Dict[str, int]] = None
_is_running = False
_lock = threading.Lock()

def _run_scrape_job():
    """Thread-safe wrapper to run the scraping job once.

    Uses a module-level lock to prevent concurrent runs and updates
    the `_last_summary` and `_is_running` status variables.
    """
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

# FRONT END ROUTES
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

# BACK END ROUTES
@app.get("/health")
def health():
    """Health endpoint returning process status and last run summary."""
    return {"ok": True, "running": _is_running, "last_summary": _last_summary}

@app.post("/run")
def run():
    """Trigger a scraping job (if not already running) and return the last summary."""
    _run_scrape_job()
    return {"status": "completed", "summary": _last_summary}

@app.get("/video/{video_id}/view", include_in_schema=False)
def video_page(video_id: str):
    """Render a simple dynamic page for a given video_id."""
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
    """Search explicit mentions by keyword or text.

    Returns recent mention rows matching `q` in the `text` or `keyword` fields,
    limited to `limit` results ordered by `created_at` descending.
    """
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

@app.get("/api/notification-settings")
def get_notification_settings():
    """
    Fetch current settings from Firestore.
    Returns keys: sender, password, recipients (CSV string for easy editing).
    """
    try:
        db = firestore.Client(database="notification-system")
        doc_ref = db.collection("settings").document("configuration")
        doc = doc_ref.get()

        if not doc.exists:
            return {
                "ok": True,
                "settings": {"sender": "", "password": "", "recipients": ""},
            }

        data = doc.to_dict() or {}

        # Convert Firestore Array -> CSV String for the UI text input
        recipients_list = data.get("recipients", [])
        recipients_str = ""
        if isinstance(recipients_list, list):
            recipients_str = ",".join(recipients_list)

        settings = {
            "sender": data.get("sender", ""),
            "password": data.get("password", ""),
            "recipients": recipients_str,
        }

        return {"ok": True, "settings": settings}

    except Exception as e:
        logger.error(f"Failed to fetch notification settings: {e}")
        return {"ok": False, "error": str(e)}

@app.post("/api/notification-settings")
async def update_notification_settings(request: Request):
    """
    Update settings in Firestore: sender, password, recipients.
    """
    try:
        body = await request.json()
        updates = {}

        # 1. Handle Sender
        if "sender" in body and body["sender"] is not None:
            val = str(body["sender"]).strip()
            if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", val) or "," in val:
                return {"ok": False, "error": "Sender must be a single valid email address."}
            updates["sender"] = val

        # 2. Handle Password
        if "password" in body and body["password"] is not None:
            val = str(body["password"]).strip()
            if any(c.isspace() for c in val):
                return {"ok": False, "error": "Password must not contain whitespaces."}
            if val:  # Only update if not empty
                updates["password"] = val

        # 3. Handle Recipients (String -> Array)
        if "recipients" in body and body["recipients"] is not None:
            val = str(body["recipients"]).strip()
            if " " in val:
                return {"ok": False, "error": "Recipients must be comma-separated with NO spaces."}

            # Convert CSV string to List
            email_list = [e.strip() for e in val.split(",") if e.strip()]

            for email in email_list:
                if "@" not in email:
                    return {"ok": False, "error": f"Invalid email in recipients: {email}"}

            updates["recipients"] = email_list

        # Write to Firestore
        if updates:
            db = firestore.Client(database="notification-system")
            doc_ref = db.collection("settings").document("configuration")
            doc_ref.set(updates, merge=True)
            logger.info("Updated Firestore settings")

        return {"ok": True, "message": "Settings updated successfully"}

    except Exception as e:
        logger.error(f"Failed to update notification settings: {e}")
        return {"ok": False, "error": str(e)}

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

# CLI ENTRYPOINT
def main():
    """CLI entrypoint: run the scraper once and log a summary."""
    try:
        scraper = YouTubeTranscriptScraper()
        summary = scraper.run_once()
        logger.info(f"Done. Summary: {summary}")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
