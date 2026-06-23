import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { PdfReader } from "../components/PdfReader";
import { VideoStage } from "../components/VideoStage";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";

/** The reading room: book page (left) and the film (right) sharing one playhead. */
export default function WorkspacePage() {
  const { id: bookId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [sessionId, setSessionId] = useState<string | null>(null);

  useEffect(() => {
    if (!bookId) return;
    let cancelled = false;
    void api
      .POST("/api/sessions", { body: { book_id: bookId, focus_word: 0, mode: "viewer" } })
      .then(({ data }) => {
        if (!cancelled && data) setSessionId(data.session_id);
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const { engine, snapshot } = useSyncEngine(sessionId);

  const { data: shots } = useQuery({
    queryKey: queryKeys.shots(bookId ?? ""),
    enabled: Boolean(bookId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/shots", {
        params: { path: { book_id: bookId as string } },
      });
      if (error || !data) throw new Error("failed to load shots");
      return data;
    },
  });

  useEffect(() => {
    if (shots) engine.setShots(shots);
  }, [shots, engine]);

  if (!bookId) return null;

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-900 px-4 py-2 text-sm">
        <button onClick={() => navigate("/")} className="text-neutral-400 hover:text-neutral-100">
          ← Library
        </button>
        <span className="text-xs text-neutral-500">
          {snapshot.owner} · {snapshot.velocity.toFixed(1)} wps · {shots?.length ?? 0} shots
        </span>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-2">
        <section className="min-h-0 border-r border-neutral-900">
          <PdfReader
            bookId={bookId}
            page={snapshot.currentPage}
            highlightWordIndex={snapshot.highlightWordIndex}
            onSeekWord={(word) => engine.seek(word, performance.now())}
          />
        </section>
        <section className="min-h-0">
          <VideoStage engine={engine} clipUrl={snapshot.currentClipUrl} />
        </section>
      </div>
    </div>
  );
}
