#!/usr/bin/env python3
# python3 scraper.py

"""
YouTube Transcript Scraper for Hawaii Legislative Channels (MVP)

MVP scope:
1) Auto-fetch new YouTube hearings (completed livestreams)
2) Auto-fetch transcripts (captions first via Apify actor)
3) Store transcripts in BigQuery as JSON
4) Deduplicate by video_id

NOTE:
- Keyword scan + output table happens in uh-mentions-collector later.
- Whisper fallback is intentionally NOT included in this scraper yet.
"""

import os
import sys
import json
import time
import logging
from typing import List, Dict, Set, Optional

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

from google.cloud import bigquery, secretmanager
from google.oauth2 import service_account

from apify_client import ApifyClient
from apify_client._errors import ApifyApiError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Env loading
# ------------------------------------------------------------------------------

def load_environment_variables():
    """Load environment variables from Secret Manager (Cloud Run) or .env (local)."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv("GCP_PROJECT_ID", "its-gro")
        secret_name = f"projects/{project_id}/secrets/youtube-scraper-env/versions/latest"

        response = client.access_secret_version(request={"name": secret_name})
        secret_payload = response.payload.data.decode("UTF-8")

        for line in secret_payload.strip().split("\n"):
            if line and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

        logger.info("✓ Loaded environment variables from Secret Manager")
        return True

    except Exception as e:
        logger.info(f"Could not load from Secret Manager ({e}), trying .env...")
        load_dotenv()
        logger.info("✓ Loaded environment variables from .env")
        return True

load_environment_variables()

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
BQ_DATASET = os.getenv("BQ_DATASET", "uh_legis")
BQ_TABLE = os.getenv("BQ_TABLE", "transcripts_json")

SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_ACCOUNT_EMAIL", "")
SERVICE_ACCOUNT_KEY_PATH = os.getenv("SERVICE_ACCOUNT_KEY_PATH", None)

MAX_VIDEOS_PER_CHANNEL = int(os.getenv("MAX_VIDEOS_PER_CHANNEL", "50"))

# Apify
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "youtube-video-transcript")  
# ^ put your exact actor ID/name here, e.g. "streamers/youtube-video-transcript"
APIFY_LANGUAGE = os.getenv("APIFY_LANGUAGE", "en")

# BigQuery insert behavior
BQ_JSON_COLUMN = os.getenv("BQ_JSON_COLUMN", "transcript_json")  
# Column that holds the full Apify payload (STRING or JSON)
BQ_JSON_AS_STRING = os.getenv("BQ_JSON_AS_STRING", "true").lower() == "true"

CHANNELS = [
    {
        "name": "Hawaii Senate",
        "channel_id": "UCekvvdL_uyq2DUyj1GjlrOA"
    },
    {
        "name": "Hawaii House of Representatives",
        "channel_id": "UCvoLAX1ww3e63K8qQ5of0bw"
    }
]

# ------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------

def validate_configuration():
    required = {
        "YOUTUBE_API_KEY": YOUTUBE_API_KEY,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "BQ_DATASET": BQ_DATASET,
        "BQ_TABLE": BQ_TABLE,
        "SERVICE_ACCOUNT_EMAIL": SERVICE_ACCOUNT_EMAIL,
        "APIFY_API_TOKEN": APIFY_API_TOKEN,
        "APIFY_ACTOR_ID": APIFY_ACTOR_ID,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("=" * 80)
        logger.error("CONFIGURATION ERROR: Missing env vars")
        for k in missing:
            logger.error(f"  ✗ {k} not set")
        logger.error("=" * 80)
        sys.exit(1)

    logger.info("✓ Configuration validated")
    logger.info(f"  - Project: {GCP_PROJECT_ID}")
    logger.info(f"  - Dataset.Table: {BQ_DATASET}.{BQ_TABLE}")
    logger.info(f"  - Apify actor: {APIFY_ACTOR_ID}")
    logger.info(f"  - JSON column: {BQ_JSON_COLUMN} (as_string={BQ_JSON_AS_STRING})")

# ------------------------------------------------------------------------------
# Scraper
# ------------------------------------------------------------------------------

class YouTubeTranscriptScraper:
    def __init__(self):
        self.youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        self.apify = ApifyClient(APIFY_API_TOKEN)
        self.bq_client = self._init_bigquery_client()
        self.table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

    def _init_bigquery_client(self) -> bigquery.Client:
        if SERVICE_ACCOUNT_KEY_PATH and os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
            credentials = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_KEY_PATH,
                scopes=["https://www.googleapis.com/auth/bigquery"]
            )
            logger.info(f"Using SA key: {SERVICE_ACCOUNT_KEY_PATH}")
            return bigquery.Client(credentials=credentials, project=GCP_PROJECT_ID)

        logger.info("Using default Cloud Run credentials")
        return bigquery.Client(project=GCP_PROJECT_ID)

    # ---------------------- BigQuery dedupe ----------------------

    def get_existing_video_ids(self) -> Set[str]:
        query = f"SELECT DISTINCT video_id FROM `{self.table_id}`"
        try:
            logger.info("Fetching existing video_ids from BigQuery...")
            results = self.bq_client.query(query).result()
            ids = {row.video_id for row in results if row.video_id}
            logger.info(f"Found {len(ids)} existing videos")
            return ids
        except Exception as e:
            logger.warning(f"Could not fetch existing IDs (continuing anyway): {e}")
            return set()

    # ---------------------- YouTube listing ----------------------

    def get_channel_videos(self, channel_id: str, channel_name: str) -> List[str]:
        video_ids: List[str] = []
        try:
            logger.info(f"Fetching completed livestreams from {channel_name}...")

            request = self.youtube.search().list(
                part="id",
                channelId=channel_id,
                eventType="completed",
                type="video",
                order="date",
                maxResults=min(MAX_VIDEOS_PER_CHANNEL, 50),
            )
            response = request.execute()

            for item in response.get("items", []):
                if item["id"]["kind"] == "youtube#video":
                    video_ids.append(item["id"]["videoId"])

            while "nextPageToken" in response and len(video_ids) < MAX_VIDEOS_PER_CHANNEL:
                request = self.youtube.search().list(
                    part="id",
                    channelId=channel_id,
                    eventType="completed",
                    type="video",
                    order="date",
                    maxResults=min(MAX_VIDEOS_PER_CHANNEL - len(video_ids), 50),
                    pageToken=response["nextPageToken"],
                )
                response = request.execute()
                for item in response.get("items", []):
                    if item["id"]["kind"] == "youtube#video":
                        video_ids.append(item["id"]["videoId"])

            logger.info(f"Found {len(video_ids)} videos in {channel_name}")
            return video_ids

        except HttpError as e:
            logger.error(f"YouTube API error for {channel_name}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing videos: {e}")
            return []

    # ---------------------- Transcript fetch ----------------------

    def _try_youtube_transcript_api(self, video_id: str) -> Optional[List[Dict]]:
        """Try native transcript API first (cheap + fast)."""
        try:
            return YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US"])
        except (TranscriptsDisabled, NoTranscriptFound):
            return None
        except Exception:
            return None

    def _run_apify_actor(self, video_url: str) -> Optional[Dict]:
        """
        Run the Apify actor with a few common input schemas.
        This avoids schema mismatch errors like:
        'Property input.videoUrls is not allowed.'
        """
        inputs_to_try = [
            {"startUrls": [{"url": video_url}], "language": APIFY_LANGUAGE},
            {"startUrl": video_url, "language": APIFY_LANGUAGE},
            {"videoUrl": video_url, "language": APIFY_LANGUAGE},
            {"url": video_url, "language": APIFY_LANGUAGE},
        ]

        last_err = None
        for actor_input in inputs_to_try:
            try:
                run = self.apify.actor(APIFY_ACTOR_ID).call(run_input=actor_input)
                dataset_id = run.get("defaultDatasetId")
                if not dataset_id:
                    last_err = "No dataset id returned"
                    continue

                items = list(self.apify.dataset(dataset_id).iterate_items())
                if not items:
                    last_err = "Apify dataset empty"
                    continue

                return items[0]

            except ApifyApiError as e:
                last_err = str(e)
                continue
            except Exception as e:
                last_err = str(e)
                continue

        logger.error(f"Apify actor failed for {video_url}. Last error: {last_err}")
        return None

    def get_transcript_payload(self, video_id: str) -> Optional[Dict]:
        """
        Returns a payload dict ready for BigQuery insert.
        Strategy:
        1) Try youtube_transcript_api (if available)
        2) Else Apify actor
        """
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        yt_segments = self._try_youtube_transcript_api(video_id)
        if yt_segments:
            logger.info(f"Native transcript OK for {video_id} ({len(yt_segments)} segments)")
            return {
                "video_id": video_id,
                "url": video_url,
                "source": "youtube_transcript_api",
                "transcript": yt_segments,
            }

        logger.info(f"No native transcript for {video_id}. Falling back to Apify...")
        apify_item = self._run_apify_actor(video_url)
        if not apify_item:
            return None

        apify_item["video_id"] = apify_item.get("video_id") or video_id
        apify_item["url"] = apify_item.get("url") or video_url
        apify_item["source"] = "apify"

        return apify_item

    # ---------------------- BigQuery upload ----------------------

    def upload_payload_to_bigquery(self, payload: Dict) -> bool:
        if not payload:
            return False

        # Ensure available_languages is a string (BQ schema conflict fix)
        langs = payload.get("available_languages", [])
        if isinstance(langs, list):
            payload["available_languages"] = ",".join(langs)

        row = {
            "video_id": payload.get("video_id"),
            "url": payload.get("url"),
            "title": payload.get("title"),
            "channel_id": payload.get("channel_id"),
            "channel_name": payload.get("channel_name"),
            "published_at": payload.get("published_at"),
            "status": payload.get("status"),
            "message": payload.get("message"),
        }

        # Store full payload as JSON
        if BQ_JSON_AS_STRING:
            row[BQ_JSON_COLUMN] = json.dumps(payload, ensure_ascii=False)
        else:
            row[BQ_JSON_COLUMN] = payload

        try:
            errors = self.bq_client.insert_rows_json(
                self.table_id,
                [row],
                ignore_unknown_values=True,  # critical for schema drift
            )
            if errors:
                logger.error(f"BQ insert errors for {payload.get('video_id')}: {errors}")
                return False

            logger.info(f"✓ Uploaded payload for {payload.get('video_id')}")
            return True

        except Exception as e:
            logger.error(f"BQ upload failed: {e}")
            return False

    # ---------------------- Pipeline ----------------------

    def process_video(self, video_id: str) -> bool:
        logger.info(f"Processing video {video_id}...")
        payload = self.get_transcript_payload(video_id)
        if not payload:
            logger.warning(f"Skipping {video_id} (no transcript)")
            return False
        return self.upload_payload_to_bigquery(payload)

    def run(self):
        existing_ids = self.get_existing_video_ids()

        processed = skipped = failed = 0

        for ch in CHANNELS:
            logger.info("=" * 80)
            logger.info(f"Channel: {ch['name']}")
            logger.info("=" * 80)

            vids = self.get_channel_videos(ch["channel_id"], ch["name"])
            new_vids = [v for v in vids if v not in existing_ids]

            skipped += (len(vids) - len(new_vids))
            logger.info(f"{len(vids)} total, {len(new_vids)} new, {len(vids)-len(new_vids)} skipped")

            for vid in new_vids:
                ok = self.process_video(vid)
                if ok:
                    processed += 1
                else:
                    failed += 1
                time.sleep(2)

        logger.info("\n" + "=" * 80)
        logger.info("SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Processed: {processed}")
        logger.info(f"Skipped:   {skipped}")
        logger.info(f"Failed:    {failed}")
        logger.info("=" * 80)


def main():
    validate_configuration()
    try:
        YouTubeTranscriptScraper().run()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
