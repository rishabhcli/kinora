import { describe, expect, it } from "vitest";

import { SessionSocket, type WebSocketLike } from "./socket";

/** A fake WebSocket that records what the socket sends over the wire. */
function fakeTransport() {
  const sent: string[] = [];
  let socket: WebSocketLike | null = null;
  const factory = (_url: string): WebSocketLike => {
    socket = {
      send: (data: string) => sent.push(data),
      close: () => undefined,
      onopen: null,
      onclose: null,
      onerror: null,
      onmessage: null,
    };
    return socket;
  };
  return { sent, factory, open: () => socket?.onopen?.({}) };
}

describe("SessionSocket.sendComment", () => {
  it("carries note, shot_id and region_png on the §5.6 comment wire", async () => {
    const { sent, factory } = fakeTransport();
    const socket = new SessionSocket({
      baseUrl: "http://api.test",
      sessionId: "s1",
      getToken: () => null,
      createWebSocket: factory,
      onEvent: () => undefined,
      reconnect: false,
    });
    await socket.connect();

    // A region-comment: note + the bound shot + the boxed region PNG.
    socket.sendComment("make her coat crimson", "shot_1", "BASE64PNG");
    expect(JSON.parse(sent[0] ?? "{}")).toEqual({
      type: "comment",
      note: "make her coat crimson",
      shot_id: "shot_1",
      region_png: "BASE64PNG",
    });

    // A note without a selection nulls the optional fields (no `undefined` holes).
    socket.sendComment("warmer light");
    expect(JSON.parse(sent[1] ?? "{}")).toEqual({
      type: "comment",
      note: "warmer light",
      shot_id: null,
      region_png: null,
    });
  });
});
