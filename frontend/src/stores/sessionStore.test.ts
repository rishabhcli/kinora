import { beforeEach, describe, expect, it } from "vitest";

import { useSessionStore } from "./sessionStore";

beforeEach(() => {
  useSessionStore.getState().reset();
});

describe("sessionStore", () => {
  it("tracks session identity, mode, reading scalars and resets", () => {
    const s = useSessionStore.getState();
    s.setSession("sess1", "book1");
    s.setMode("director");
    s.setReading(42, 6.2);
    s.setCommitted(50);

    const st = useSessionStore.getState();
    expect(st.sessionId).toBe("sess1");
    expect(st.bookId).toBe("book1");
    expect(st.mode).toBe("director");
    expect(st.focusWord).toBe(42);
    expect(st.velocity).toBeCloseTo(6.2);
    expect(st.committedSecondsAhead).toBe(50);

    useSessionStore.getState().reset();
    const cleared = useSessionStore.getState();
    expect(cleared.sessionId).toBeNull();
    expect(cleared.mode).toBe("viewer");
    expect(cleared.committedSecondsAhead).toBe(0);
  });
});
