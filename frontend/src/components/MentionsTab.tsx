import { useState, useEffect } from "react";
import { listMentions, searchMentions } from "@/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

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

interface MentionsTabProps {
  searchQuery: string;
}

export function MentionsTab({ searchQuery }: MentionsTabProps) {
  const [mentions, setMentions] = useState<Mention[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchMentions() {
      try {
        setLoading(true);
        if (searchQuery.trim()) {
          const data = await searchMentions(searchQuery);
          setMentions(data.results);
        } else {
          const data = await listMentions();
          setMentions(data.results);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load mentions");
      } finally {
        setLoading(false);
      }
    }
    fetchMentions();
  }, [searchQuery]);

  if (loading) {
    return <div className="text-center py-12 text-muted-foreground">Loading mentions...</div>;
  }

  if (error) {
    return <div className="text-center py-12 text-destructive">Error: {error}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted-foreground">
        {mentions.length} {mentions.length === 1 ? "mention" : "mentions"} found
      </div>
      <div className="space-y-3">
        {mentions.map((mention, index) => (
          <Card key={`${mention.video_id}-${mention.segment_index}-${index}`} className="p-4">
            <div className="space-y-3">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-foreground line-clamp-1 mb-1">
                    {mention.video_name}
                  </h3>
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant="secondary" className="text-xs">
                      {mention.keyword}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {Math.floor(mention.start_sec / 60)}:{String(Math.floor(mention.start_sec % 60)).padStart(2, '0')}
                    </span>
                  </div>
                </div>
                <Button
                  size="sm"
                  asChild
                >
                  <a
                    href={`https://daily-youtube-scraper-655654578945.us-central1.run.app/video/${mention.video_id}/view?t=${mention.start_sec}`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    View Video
                  </a>
                </Button>
              </div>
              <p className="text-sm text-foreground/90 leading-relaxed">
                {mention.text}
              </p>
            </div>
          </Card>
        ))}
      </div>
      {mentions.length === 0 && (
        <div className="text-center py-12 text-muted-foreground">
          {searchQuery ? `No mentions found for "${searchQuery}"` : "No mentions available"}
        </div>
      )}
    </div>
  );
}
