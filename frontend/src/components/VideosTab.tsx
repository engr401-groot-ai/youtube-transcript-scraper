import { useState, useEffect } from "react";
import { listVideos } from "@/api";
import { Card } from "@/components/ui/card";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Video {
  video_id: string;
  video_url: string;
  video_name: string;
  mention_count: number;
}

interface VideosTabProps {
  searchQuery: string;
}

export function VideosTab({ searchQuery }: VideosTabProps) {
  const [videos, setVideos] = useState<Video[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchVideos() {
      try {
        setLoading(true);
        const data = await listVideos();
        setVideos(data.results);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load videos");
      } finally {
        setLoading(false);
      }
    }
    fetchVideos();
  }, []);

  const filteredVideos = videos.filter((video) =>
    video.video_name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  if (loading) {
    return <div className="text-center py-12 text-muted-foreground">Loading videos...</div>;
  }

  if (error) {
    return <div className="text-center py-12 text-destructive">Error: {error}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted-foreground">
        Showing {filteredVideos.length} of {videos.length} videos
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {filteredVideos.map((video) => (
          <Card key={video.video_id} className="p-4 space-y-3">
            <div>
              <h3 className="font-semibold text-foreground line-clamp-2 mb-1">
                {video.video_name}
              </h3>
              <p className="text-xs text-muted-foreground font-mono">{video.video_id}</p>
            </div>
            <div className="text-sm text-muted-foreground">
              {video.mention_count} {video.mention_count === 1 ? "mention" : "mentions"}
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                asChild
                className="flex-1"
              >
                <a
                  href={video.video_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-center gap-1"
                >
                  <ExternalLink className="h-3 w-3" />
                  YouTube
                </a>
              </Button>
              <Button
                size="sm"
                asChild
                className="flex-1"
              >
                <a
                  href={`https://daily-youtube-scraper-655654578945.us-central1.run.app/video/${video.video_id}/view`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View More
                </a>
              </Button>
            </div>
          </Card>
        ))}
      </div>
      {filteredVideos.length === 0 && (
        <div className="text-center py-12 text-muted-foreground">
          No videos found matching "{searchQuery}"
        </div>
      )}
    </div>
  );
}
