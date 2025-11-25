import { useState } from "react";
import { VideosTab } from "@/components/VideosTab";
import { MentionsTab } from "@/components/MentionsTab";
import { KeywordsModal } from "@/components/KeywordsModal";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Search, Tag } from "lucide-react";

const Index = () => {
  const [activeTab, setActiveTab] = useState("videos");
  const [searchQuery, setSearchQuery] = useState("");
  const [keywordsOpen, setKeywordsOpen] = useState(false);

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b bg-card">
        <div className="container mx-auto px-4 py-4">
          <h1 className="text-2xl font-bold text-foreground mb-4">YouTube Video Dashboard</h1>
          <div className="flex gap-3 flex-col sm:flex-row">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                type="text"
                placeholder={activeTab === "videos" ? "Filter videos..." : "Search mentions..."}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-9"
              />
            </div>
            <Button
              variant="outline"
              onClick={() => setKeywordsOpen(true)}
              className="sm:w-auto"
            >
              <Tag className="h-4 w-4 mr-2" />
              Keywords
            </Button>
          </div>
        </div>
      </header>

      <main className="container mx-auto px-4 py-6">
        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList className="mb-6">
            <TabsTrigger value="videos">Videos</TabsTrigger>
            <TabsTrigger value="mentions">Mentions</TabsTrigger>
          </TabsList>
          
          <TabsContent value="videos">
            <VideosTab searchQuery={searchQuery} />
          </TabsContent>
          
          <TabsContent value="mentions">
            <MentionsTab searchQuery={searchQuery} />
          </TabsContent>
        </Tabs>
      </main>

      <KeywordsModal open={keywordsOpen} onOpenChange={setKeywordsOpen} />
    </div>
  );
};

export default Index;
