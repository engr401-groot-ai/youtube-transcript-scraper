const BASE_URL = "https://daily-youtube-scraper-655654578945.us-central1.run.app";

interface Video {
  video_id: string;
  video_url: string;
  video_name: string;
  mention_count: number;
}

interface Mention {
  video_id: string;
  segment_index: number;
  start_sec: number;
  end_sec: number;
  text: string;
  keyword: string;
  video_name: string;
  video_url: string;
  timestamp_url: string;
  created_at: string;
}

export async function listVideos(limit = 100): Promise<{ count: number; results: Video[] }> {
  const response = await fetch(`${BASE_URL}/list-videos?limit=${limit}`);
  if (!response.ok) throw new Error("Failed to fetch videos");
  return response.json();
}

export async function listMentions(limit = 100): Promise<{ count: number; results: Mention[] }> {
  const response = await fetch(`${BASE_URL}/list_mentions?limit=${limit}`);
  if (!response.ok) throw new Error("Failed to fetch mentions");
  return response.json();
}

export async function searchMentions(query: string, limit = 50): Promise<{ count: number; results: Mention[] }> {
  const response = await fetch(`${BASE_URL}/search?q=${encodeURIComponent(query)}&limit=${limit}`);
  if (!response.ok) throw new Error("Failed to search mentions");
  return response.json();
}

export async function getKeywords(): Promise<{ count: number; keywords: string[] }> {
  const response = await fetch(`${BASE_URL}/keywords`);
  if (!response.ok) throw new Error("Failed to fetch keywords");
  return response.json();
}

export function getVideoViewUrl(videoId: string, startSec?: number): string {
  const url = `${BASE_URL}/video/${videoId}/view`;
  return startSec !== undefined ? `${url}?t=${startSec}` : url;
}
