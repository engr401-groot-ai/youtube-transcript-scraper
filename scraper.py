#!/usr/bin/env python3
"""
YouTube Transcript Scraper for Hawaii Legislative Channels

Modes:
1) Cloud Run service (default): exposes HTTP endpoints:
   - GET /health
   - POST /run  (kicks off a scrape + BQ upload)
2) CLI/local: `python scraper.py` runs a scrape once and exits.

Scrapes completed live stream videos from Hawaii Senate and House YouTube channels,
extracts transcripts (English if available), and uploads them to Google BigQuery
with deduplication.

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
from typing import List, Dict, Set, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from google.cloud import bigquery, secretmanager
from google.oauth2 import service_account

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("scraper")

app = FastAPI(title="UH YouTube Transcript Scraper", version="0.2.0")


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
SERVICE_ACCOUNT_KEY_PATH = os.getenv("SERVICE_ACCOUNT_KEY_PATH")  # optional local
MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "50"))

CHANNELS = [
    {
        "name": "Hawaii Senate",
        "channel_id": "UCekvvdL_uyq2DUyj1GjlrOA",
    },
    {
        "name": "Hawaii House of Representatives",
        "channel_id": "UCvoLAX1ww3e63K8qQ5of0bw",
    },
]


def validate_configuration() -> None:
    required = {
        "YOUTUBE_API_KEY": YOUTUBE_API_KEY,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "BQ_DATASET": BQ_DATASET,
        "BQ_TABLE": BQ_TABLE,
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

    def get_transcript(self, video_id: str) -> List[Dict]:
        try:
            return YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
        except (TranscriptsDisabled, NoTranscriptFound):
            return []
        except Exception as primary:
            logger.info(f"Direct transcript failed for {video_id}: {primary}; trying fallback")
            try:
                transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
                candidates = []

                for finder in (
                    lambda: transcripts.find_manually_created_transcript(["en", "en-US"]),
                    lambda: transcripts.find_generated_transcript(["en", "en-US"]),
                    lambda: transcripts.find_transcript(["en", "en-US"]),
                ):
                    try:
                        candidates.append(finder())
                    except Exception:
                        pass

                for cand in candidates:
                    try:
                        return cand.fetch()
                    except Exception:
                        continue

            except Exception as fallback:
                logger.warning(f"Fallback list_transcripts failed for {video_id}: {fallback}")

            return []

    def upload_to_bigquery(self, video_id: str, transcript: List[Dict]) -> bool:
        if not transcript:
            return False

        rows = []
        for idx, seg in enumerate(transcript):
            start = float(seg.get("start", 0))
            dur = float(seg.get("duration", 0))
            rows.append(
                {
                    "video_id": video_id,
                    "segment_index": idx,
                    "start_sec": int(start),
                    "end_sec": int(start + dur),
                    "text": seg.get("text", ""),
                }
            )

        try:
            errors = self.bq_client.insert_rows_json(self.table_id, rows)
            if errors:
                logger.error(f"BQ insert errors for {video_id}: {errors}")
                return False

            logger.info(f"Uploaded {len(rows)} segments for {video_id}")
            return True

        except Exception as e:
            logger.error(f"BQ upload error for {video_id}: {e}")
            return False

    def process_video(self, video_id: str) -> bool:
        transcript = self.get_transcript(video_id)
        if not transcript:
            logger.info(f"No transcript for {video_id}; skipping")
            return False
        return self.upload_to_bigquery(video_id, transcript)

    def run_once(self) -> Dict[str, int]:
        existing = self.get_existing_video_ids()

        processed = skipped = failed = 0

        for ch in CHANNELS:
            vids = self.get_channel_videos(ch["channel_id"], ch["name"])
            new_vids = [v for v in vids if v not in existing]
            skipped += len(vids) - len(new_vids)

            for v in new_vids:
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
def run(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_scrape_job)
    return {"started": True, "running": True}


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
