import { useState, useEffect } from "react";
import { getKeywords } from "@/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface KeywordsModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function KeywordsModal({ open, onOpenChange }: KeywordsModalProps) {
  const [keywords, setKeywords] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      async function fetchKeywords() {
        try {
          setLoading(true);
          setError(null);
          const data = await getKeywords();
          setKeywords(data.keywords);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Failed to load keywords");
        } finally {
          setLoading(false);
        }
      }
      fetchKeywords();
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Keywords</DialogTitle>
        </DialogHeader>
        <div className="mt-4">
          {loading && (
            <div className="text-center py-8 text-muted-foreground">Loading keywords...</div>
          )}
          {error && (
            <div className="text-center py-8 text-destructive">Error: {error}</div>
          )}
          {!loading && !error && (
            <div className="space-y-2">
              <div className="text-sm text-muted-foreground">
                {keywords.length} {keywords.length === 1 ? "keyword" : "keywords"}
              </div>
              <div className="text-sm leading-relaxed">
                {keywords.join(", ")}
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
