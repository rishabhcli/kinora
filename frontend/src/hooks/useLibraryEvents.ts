import { useEffect } from "react";

import { libraryEventsUrl } from "../api/client";
import { useEventsStore } from "../stores/eventsStore";
import { GenerationClient } from "../sync/GenerationClient";

/** Subscribe to live shelf ingest progress over SSE (§5.1/§5.6). */
export function useLibraryEvents(enabled: boolean): void {
  const push = useEventsStore((s) => s.push);

  useEffect(() => {
    if (!enabled) return undefined;
    const client = new GenerationClient({
      sessionId: "library",
      eventsUrl: libraryEventsUrl(),
      onEvent: push,
      reconnect: true,
    });
    client.connect();
    return () => client.close();
  }, [enabled, push]);
}
