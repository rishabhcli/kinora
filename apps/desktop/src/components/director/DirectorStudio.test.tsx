import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import DirectorStudio from "./DirectorStudio";
import type { Book } from "../../data/books";
import { __resetStoresForTests } from "../../lib/api/stores";

// Override only the NETWORK `director` object; keep the real pure helpers
// (buildSceneLanes, etc.) that the timeline/canon components import.
const getShots = vi.fn();
const getCanon = vi.fn();
vi.mock("../../lib/api/director", async (orig) => {
  const actual = await orig<typeof import("../../lib/api/director")>();
  return {
    ...actual,
    director: {
      getShots: (...a: unknown[]) => getShots(...a),
      getCanon: (...a: unknown[]) => getCanon(...a),
      getConflicts: vi.fn().mockResolvedValue([]),
      getBookStyle: vi.fn().mockResolvedValue({ scope: "book", book_id: "b1", priors: [] }),
      getMyStyle: vi.fn().mockResolvedValue({ scope: "user", book_id: null, priors: [] }),
    },
  };
});

// Mock the base api: a session create + a no-op SSE subscription.
const createSession = vi.fn();
const openSessionEvents = vi.fn((_sessionId: string, _onEvent: (e: unknown) => void) => () => {});
vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      createSession: (bookId: string, focusWord?: number) => createSession(bookId, focusWord ?? 0),
      openSessionEvents: (sessionId: string, onEvent: (e: unknown) => void) =>
        openSessionEvents(sessionId, onEvent),
    },
  };
});

const book: Book = {
  id: "b1",
  title: "Moby Dick",
  author: "Melville",
  progress: 40,
  coverColor: "#000",
  coverGradient: "g",
  coverImage: "",
  textColor: "#fff",
  spineColor: "#000",
  live: true,
};

beforeEach(() => {
  __resetStoresForTests();
  getShots.mockReset();
  getCanon.mockReset();
  createSession.mockReset();
  openSessionEvents.mockClear();
  getShots.mockResolvedValue([
    { shot_id: "s1", beat_id: "b1", scene_id: "sc1", source_span: { word_range: [0, 50] }, status: "accepted", render_mode: "r2v", duration_s: 5, qa: null, clip_url: "http://minio:9000/kinora/c.mp4", reference_image_ids: [] },
  ]);
  getCanon.mockResolvedValue({ book_id: "b1", entities: [], states: [], markdown: null });
});

describe("DirectorStudio", () => {
  it("loads shots + canon and renders the timeline tab", async () => {
    render(<DirectorStudio book={book} library={[]} onClose={() => {}} />);
    expect(screen.getByText("Moby Dick")).toBeInTheDocument();
    await waitFor(() => expect(getShots).toHaveBeenCalledWith("b1"));
    expect(getCanon).toHaveBeenCalledWith("b1");
    expect(await screen.findByText(/1 shots · 1 scenes/i)).toBeInTheDocument();
  });

  it("switches tabs", async () => {
    render(<DirectorStudio book={book} library={[]} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Canon" }));
    await waitFor(() => expect(screen.getByText(/0 entities/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "Conflicts" }));
    expect(await screen.findByText(/start a session to see and resolve|no conflicts/i)).toBeInTheDocument();
  });

  it("opens a session on demand", async () => {
    createSession.mockResolvedValue({ session_id: "sess-1", book_id: "b1", focus_word: 0, velocity_wps: 0, mode: "director", committed_seconds_ahead: 0 });
    render(<DirectorStudio book={book} library={[]} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /start session/i }));
    await waitFor(() => expect(createSession).toHaveBeenCalledWith("b1", 0));
    expect(await screen.findByText(/session live/i)).toBeInTheDocument();
    // SSE subscription opened for the new session
    await waitFor(() => expect(openSessionEvents).toHaveBeenCalledWith("sess-1", expect.any(Function)));
  });

  it("calls onClose from the back button", () => {
    const onClose = vi.fn();
    render(<DirectorStudio book={book} library={[]} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: /close director studio/i }));
    expect(onClose).toHaveBeenCalled();
  });
});
