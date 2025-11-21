#!/usr/bin/env python3
# python3 scraper.py

"""
YouTube Transcript Scraper for Hawaii Legislative Channels

Scrapes completed live stream videos from Hawaii Senate and House YouTube channels,
extracts transcripts, and uploads them to Google BigQuery with deduplication.
"""

import os
import sys
import logging
import time
from datetime import datetime
from typing import List, Dict, Set
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from google.cloud import bigquery
from google.oauth2 import service_account
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY', '')
GCP_PROJECT_ID = os.getenv('GCP_PROJECT_ID', '')
BQ_DATASET = os.getenv('BQ_DATASET', '')
BQ_TABLE = os.getenv('BQ_TABLE', '')
SERVICE_ACCOUNT_EMAIL = os.getenv('SERVICE_ACCOUNT_EMAIL', '')
SERVICE_ACCOUNT_KEY_PATH = os.getenv('SERVICE_ACCOUNT_KEY_PATH', None)  # Optional for local testing
MAX_VIDEOS_PER_CHANNEL = 10

# YouTube channels to scrape
CHANNELS = [
    {
        'name': 'Hawaii Senate',
        'url': 'https://www.youtube.com/@hawaiisenate/streams',
        'channel_id': 'UCekvvdL_uyq2DUyj1GjlrOA'
    },
    {
        'name': 'Hawaii House of Representatives',
        'url': 'https://www.youtube.com/@hawaiihouseofrepresentatives/streams',
        'channel_id': 'UCvoLAX1ww3e63K8qQ5of0bw'
    }
]


def validate_configuration():
    """Validate that all required configuration variables are set"""
    required_configs = {
        'YOUTUBE_API_KEY': YOUTUBE_API_KEY,
        'GCP_PROJECT_ID': GCP_PROJECT_ID,
        'BQ_DATASET': BQ_DATASET,
        'BQ_TABLE': BQ_TABLE,
        'SERVICE_ACCOUNT_EMAIL': SERVICE_ACCOUNT_EMAIL,
    }
    
    missing_configs = [name for name, value in required_configs.items() if not value]
    
    if missing_configs:
        logger.error("=" * 80)
        logger.error("CONFIGURATION ERROR: Missing required environment variables")
        logger.error("=" * 80)
        for config_name in missing_configs:
            logger.error(f"  ✗ {config_name} is not set")
        logger.error("")
        logger.error("Please set the following environment variables:")
        logger.error("  export YOUTUBE_API_KEY='your_api_key'")
        logger.error("  export GCP_PROJECT_ID='its-gro'")
        logger.error("  export BQ_DATASET='uh_legis'")
        logger.error("  export BQ_TABLE='transcripts'")
        logger.error("  export SERVICE_ACCOUNT_EMAIL='groot-ai@its-gro.iam.gserviceaccount.com'")
        logger.error("")
        logger.error("Or create a .env file with these values.")
        logger.error("=" * 80)
        sys.exit(1)
    
    logger.info("✓ Configuration validated successfully")
    logger.info(f"  - Project: {GCP_PROJECT_ID}")
    logger.info(f"  - Dataset: {BQ_DATASET}")
    logger.info(f"  - Table: {BQ_TABLE}")
    logger.info(f"  - Service Account: {SERVICE_ACCOUNT_EMAIL}")


class YouTubeTranscriptScraper:
    """Scrapes YouTube transcripts and uploads to BigQuery"""
    
    def __init__(self):
        """Initialize YouTube and BigQuery clients"""
        self.youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        self.bq_client = self._init_bigquery_client()
        self.table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
        
    def _init_bigquery_client(self) -> bigquery.Client:
        """Initialize BigQuery client with service account or default credentials"""
        try:
            if SERVICE_ACCOUNT_KEY_PATH and os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
                # Local testing with service account key file
                credentials = service_account.Credentials.from_service_account_file(
                    SERVICE_ACCOUNT_KEY_PATH,
                    scopes=["https://www.googleapis.com/auth/bigquery"]
                )
                logger.info(f"Using service account key from: {SERVICE_ACCOUNT_KEY_PATH}")
                return bigquery.Client(credentials=credentials, project=GCP_PROJECT_ID)
            else:
                # Cloud Run - uses the service account identity automatically
                logger.info("Using default credentials (Cloud Run service account)")
                return bigquery.Client(project=GCP_PROJECT_ID)
        except Exception as e:
            logger.error(f"Failed to initialize BigQuery client: {e}")
            raise
    
    def get_existing_video_ids(self) -> Set[str]:
        """Query BigQuery for already-processed video IDs"""
        query = f"""
            SELECT DISTINCT video_id
            FROM `{self.table_id}`
        """
        try:
            logger.info("Fetching existing video IDs from BigQuery...")
            query_job = self.bq_client.query(query)
            results = query_job.result()
            video_ids = {row.video_id for row in results}
            logger.info(f"Found {len(video_ids)} existing videos in BigQuery")
            return video_ids
        except Exception as e:
            logger.error(f"Error fetching existing video IDs: {e}")
            # Return empty set to continue processing (will attempt to insert all)
            return set()
    
    def get_channel_videos(self, channel_id: str, channel_name: str) -> List[str]:
        """Fetch completed live stream video IDs from a YouTube channel"""
        video_ids = []
        
        try:
            # First, get the channel's upload playlist ID
            logger.info(f"Fetching videos from {channel_name}...")
            
            # Search for completed live streams
            request = self.youtube.search().list(
                part='id,snippet',
                channelId=channel_id,
                eventType='completed',
                type='video',
                order='date',
                maxResults=min(MAX_VIDEOS_PER_CHANNEL, 50)  # API limit is 50 per request
            )
            
            response = request.execute()
            
            for item in response.get('items', []):
                if item['id']['kind'] == 'youtube#video':
                    video_ids.append(item['id']['videoId'])
            
            # If we need more videos, paginate
            while 'nextPageToken' in response and len(video_ids) < MAX_VIDEOS_PER_CHANNEL:
                request = self.youtube.search().list(
                    part='id,snippet',
                    channelId=channel_id,
                    eventType='completed',
                    type='video',
                    order='date',
                    maxResults=min(MAX_VIDEOS_PER_CHANNEL - len(video_ids), 50),
                    pageToken=response['nextPageToken']
                )
                response = request.execute()
                
                for item in response.get('items', []):
                    if item['id']['kind'] == 'youtube#video':
                        video_ids.append(item['id']['videoId'])
            
            logger.info(f"Found {len(video_ids)} videos from {channel_name}")
            return video_ids
            
        except HttpError as e:
            logger.error(f"YouTube API error for {channel_name}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching videos from {channel_name}: {e}")
            return []
    
    def get_transcript(self, video_id: str) -> List[Dict]:
        """Extract transcript from a YouTube video"""
        try:
            # Create API instance and fetch transcript
            ytt_api = YouTubeTranscriptApi()
            fetched_transcript = ytt_api.fetch(video_id, languages=['en'])
            
            # Convert FetchedTranscript to raw data (list of dicts)
            transcript_list = fetched_transcript.to_raw_data()
            
            logger.info(f"Successfully fetched transcript for video {video_id} ({len(transcript_list)} segments)")
            return transcript_list
        except TranscriptsDisabled:
            logger.warning(f"Transcripts disabled for video {video_id}")
            return []
        except NoTranscriptFound:
            logger.warning(f"No transcript found for video {video_id}")
            return []
        except Exception as e:
            logger.error(f"Error fetching transcript for video {video_id}: {e}")
            return []
    
    def upload_to_bigquery(self, video_id: str, transcript: List[Dict]) -> bool:
        """Upload transcript segments to BigQuery"""
        if not transcript:
            return False
        
        # Transform transcript data to match BigQuery schema
        rows_to_insert = []
        for idx, segment in enumerate(transcript):
            row = {
                'video_id': video_id,
                'segment_index': idx,
                'start_sec': int(segment['start']),
                'end_sec': int(segment['start'] + segment['duration']),
                'text': segment['text'],
                # created_at will be auto-populated by BigQuery with CURRENT_TIMESTAMP
            }
            rows_to_insert.append(row)
        
        try:
            errors = self.bq_client.insert_rows_json(self.table_id, rows_to_insert)
            
            if errors:
                logger.error(f"Errors inserting rows for video {video_id}: {errors}")
                return False
            else:
                logger.info(f"Successfully uploaded {len(rows_to_insert)} segments for video {video_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error uploading to BigQuery for video {video_id}: {e}")
            return False
    
    def process_video(self, video_id: str) -> bool:
        """Process a single video: fetch transcript and upload to BigQuery"""
        logger.info(f"Processing video: {video_id}")
        
        # Get transcript
        transcript = self.get_transcript(video_id)
        if not transcript:
            logger.warning(f"Skipping video {video_id} - no transcript available")
            return False
        
        # Upload to BigQuery
        success = self.upload_to_bigquery(video_id, transcript)
        return success
    
    def run(self):
        """Main execution: scrape channels and upload transcripts"""
        logger.info("=" * 80)
        logger.info("Starting YouTube Transcript Scraper")
        logger.info("=" * 80)
        
        # Get existing video IDs for deduplication
        existing_video_ids = self.get_existing_video_ids()
        
        total_videos_processed = 0
        total_videos_skipped = 0
        total_videos_failed = 0
        
        # Process each channel
        for channel in CHANNELS:
            logger.info(f"\n{'=' * 80}")
            logger.info(f"Processing channel: {channel['name']}")
            logger.info(f"{'=' * 80}")
            
            # Get video IDs from channel
            video_ids = self.get_channel_videos(channel['channel_id'], channel['name'])
            
            # Filter out already-processed videos
            new_video_ids = [vid for vid in video_ids if vid not in existing_video_ids]
            skipped_count = len(video_ids) - len(new_video_ids)
            
            logger.info(f"Found {len(video_ids)} total videos, {len(new_video_ids)} new, {skipped_count} already processed")
            total_videos_skipped += skipped_count
            
            # Process each new video
            for video_id in new_video_ids:
                success = self.process_video(video_id)
                if success:
                    total_videos_processed += 1
                else:
                    total_videos_failed += 1
                
                # Add delay to avoid rate limiting
                time.sleep(2)
        
        # Summary
        logger.info("\n" + "=" * 80)
        logger.info("SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Total videos processed: {total_videos_processed}")
        logger.info(f"Total videos skipped (already in DB): {total_videos_skipped}")
        logger.info(f"Total videos failed: {total_videos_failed}")
        logger.info("=" * 80)


def main():
    """Entry point"""
    # Validate configuration before starting
    validate_configuration()
    
    try:
        scraper = YouTubeTranscriptScraper()
        scraper.run()
        logger.info("Scraper completed successfully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
