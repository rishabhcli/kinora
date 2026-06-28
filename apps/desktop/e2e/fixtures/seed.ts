// Deterministic seed data for the network-mocked E2E runs.
//
// Shapes mirror the backend response contracts the renderer consumes
// (see apps/desktop/src/lib/api.ts: BookResponse / PageResponse / ShotResponse /
// SessionResponse). The mock backend (e2e/mocks/apiMock.ts) serves exactly these,
// so specs are reproducible byte-for-byte regardless of whether the real FastAPI
// stack is up. KINORA_LIVE_VIDEO stays OFF — clips here are bundled Ken-Burns
// fallbacks, never live Wan output.

export interface SeedBook {
  id: string;
  title: string;
  author: string | null;
  status: string;
  num_pages: number | null;
  art_direction: string | null;
  created_at: string | null;
  progress: number | null;
  stage: string | null;
}

export interface SeedWordBox {
  word_index: number;
  text: string;
  bbox: [number, number, number, number];
}

export interface SeedPage {
  book_id: string;
  page_number: number;
  image_url: string | null;
  text: string | null;
  word_boxes: SeedWordBox[] | null;
}

export interface SeedShot {
  shot_id: string;
  status: string;
  duration_s: number | null;
  clip_url: string | null;
  source_span: { page?: number; para?: number; word_range: [number, number] } | null;
  scene_id?: string | null;
  beat_id?: string | null;
}

const NOW = "2026-01-01T00:00:00Z";

/** A small, stable library: one ready public-domain book + a couple importing/ready. */
export const SEED_BOOKS: SeedBook[] = [
  {
    id: "seed-frog-king",
    title: "The Frog-King",
    author: "Brothers Grimm (public domain)",
    status: "ready",
    num_pages: 6,
    art_direction: "storybook watercolor",
    created_at: NOW,
    progress: 1.0,
    stage: "ready",
  },
  {
    id: "seed-call-of-the-wild",
    title: "The Call of the Wild",
    author: "Jack London",
    status: "ready",
    num_pages: 12,
    art_direction: "rugged naturalism",
    created_at: NOW,
    progress: 0.35,
    stage: "ready",
  },
  {
    id: "seed-importing",
    title: "A Study in Scarlet",
    author: "Arthur Conan Doyle",
    status: "importing",
    num_pages: null,
    art_direction: null,
    created_at: NOW,
    progress: 0.4,
    stage: "still importing — large book",
  },
];

/** Build a page of normalized word boxes from a sentence. */
function pageFrom(bookId: string, n: number, text: string): SeedPage {
  const words = text.split(/\s+/).filter(Boolean);
  let acc = 0;
  const word_boxes: SeedWordBox[] = words.map((w, i) => {
    const x = (acc % 10) / 10;
    const y = Math.floor(acc / 10) / 20;
    acc += 1;
    return { word_index: (n - 1) * 200 + i, text: w, bbox: [x, y, 0.08, 0.03] };
  });
  return { book_id: bookId, page_number: n, image_url: null, text, word_boxes };
}

export const SEED_PAGES: Record<string, SeedPage[]> = {
  "seed-frog-king": [
    pageFrom(
      "seed-frog-king",
      1,
      "In olden times when wishing still helped one, there lived a king whose daughters were all beautiful, but the youngest was so beautiful that the sun itself was astonished whenever it shone in her face.",
    ),
    pageFrom(
      "seed-frog-king",
      2,
      "Close by the king's castle lay a great dark forest, and under an old lime tree in the forest was a well, and when the day was very warm, the king's child went out into the forest and sat down by the side of the cool fountain.",
    ),
  ],
  "seed-call-of-the-wild": [
    pageFrom(
      "seed-call-of-the-wild",
      1,
      "Buck did not read the newspapers, or he would have known that trouble was brewing, not alone for himself, but for every tide-water dog, strong of muscle and with warm, long hair, from Puget Sound to San Diego.",
    ),
  ],
};

export const SEED_SHOTS: Record<string, SeedShot[]> = {
  "seed-frog-king": [
    {
      shot_id: "shot-fk-1",
      status: "ready",
      duration_s: 6,
      clip_url: "/generated/film-01.mp4",
      source_span: { page: 1, para: 0, word_range: [0, 40] },
      scene_id: "scene-1",
      beat_id: "beat-1",
    },
    {
      shot_id: "shot-fk-2",
      status: "ready",
      duration_s: 6,
      clip_url: "/generated/film-02.mp4",
      source_span: { page: 1, para: 1, word_range: [40, 80] },
      scene_id: "scene-1",
      beat_id: "beat-2",
    },
  ],
  "seed-call-of-the-wild": [
    {
      shot_id: "shot-cw-1",
      status: "ready",
      duration_s: 6,
      clip_url: "/generated/film-03.mp4",
      source_span: { page: 1, para: 0, word_range: [0, 40] },
      scene_id: "scene-1",
      beat_id: "beat-1",
    },
  ],
};

export const DEMO_CREDENTIALS = {
  email: "demo@kinora.local",
  password: "demo-password-123",
} as const;

/** A fake JWT (three dot-separated base64url segments). Never a real token. */
export const FAKE_TOKEN =
  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJlMmUtZGVtbyIsImV4cCI6OTk5OTk5OTk5OX0.e2e-signature-not-real";
