import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";

/**
 * The reading room. Creates a session, loads the shot timeline into the
 * SyncEngine, and (next) renders the two-pane PDF<->video workspace. For now it
 * surfaces the live playhead state so the data path is verifiable end to end.
 */
export default function WorkspacePage() {
  const { id: bookId } = useParams<{ id: string }>();
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

  return (
    <div className="grid h-screen grid-cols-2 bg-neutral-950 text-neutral-100">
      <section className="flex flex-col border-r border-neutral-900 p-6">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">Page</h2>
        <div className="mt-2 text-sm text-neutral-300">
          page {snapshot.currentPage} · focus word {snapshot.focusWord} · highlight{" "}
          {snapshot.highlightWordIndex ?? "—"}
        </div>
        <p className="mt-4 text-xs text-neutral-600">PDF reader lands next.</p>
      </section>
      <section className="flex flex-col p-6">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">Film</h2>
        <div className="mt-2 text-sm text-neutral-300">
          shot {snapshot.currentShotId ?? "—"} · {snapshot.owner} ·{" "}
          {snapshot.velocity.toFixed(1)} wps
        </div>
        <p className="mt-4 text-xs text-neutral-600">
          {sessionId ? `session ${sessionId}` : "starting session…"} · {shots?.length ?? 0} shots
        </p>
        <p className="mt-1 text-xs text-neutral-600">Video stage lands next.</p>
      </section>
    </div>
  );
}
