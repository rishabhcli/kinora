// UploadBook (Agent 05, WS3) — first-class "upload your own EPUB/PDF": HTML5
// drag-and-drop + click-to-pick, friendly validation, optimistic shelf
// placeholder, and live ingest-status polling. Wires through `lib/api/library`.
//
// The native Electron Cmd+O picker (main.ts already filters PDF/EPUB) needs a
// preload bridge to hand the renderer the file *bytes* — requested from Agent 12
// (coordination/requests/agent-05.md). Drag-drop + the in-app picker below need
// no Electron change and cover the flow end-to-end today.
import { useCallback, useId, useRef, useState } from "react";
import { announce } from "../a11y/announce";
import { ApiError } from "../lib/api";
import {
  pollBookUntilReady,
  uploadBook,
  type LibraryBook,
} from "../lib/api/library";

const MAX_BYTES = 1024 * 1024 * 1024; // 1 GB, mirrors backend MAX_PDF_BYTES
const MAX_MB = 1024;
// EPUB + PDF are fully supported end-to-end (ingest, page render, video sync).
// Other formats (txt, mobi, azw3, html, docx) are accepted via the picker but
// the backend rejects them; the validator surfaces a friendly message.
const ACCEPT_SUPPORTED = [".epub", ".pdf"];
const ACCEPT_ALL = [".epub", ".pdf", ".txt", ".mobi", ".azw3", ".html", ".htm", ".docx"];

export interface UploadItem {
  key: string;
  title: string;
  status: "uploading" | "importing" | "ready" | "error";
  progress: number; // 0..100
  stage?: string;
  book?: LibraryBook;
  error?: string;
}

interface UploadBookProps {
  /** Surfaces in-flight uploads so the library can show optimistic placeholders. */
  onUploadsChange?: (items: UploadItem[]) => void;
  /** Fired when a book finishes importing (parent re-fetches the shelf). */
  onReady?: (book: LibraryBook) => void;
}

function fmtSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)}GB`;
  return `${Math.round(bytes / 1024 / 1024)}MB`;
}

function validate(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPT_ALL.some((ext) => name.endsWith(ext))) {
    return "Unsupported file type. Try EPUB or PDF.";
  }
  if (!ACCEPT_SUPPORTED.some((ext) => name.endsWith(ext))) {
    return "Only EPUB or PDF are supported today — more formats are coming.";
  }
  if (file.size > MAX_BYTES) {
    return `That file is ${fmtSize(file.size)} — the limit is 1GB.`;
  }
  if (file.size < 200) return "That file looks empty or corrupt.";
  return null;
}

function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 413 && e.detail.includes("page")) return "Over the per-book page limit.";
    if (e.status === 413) return `Over the 1GB upload limit.`;
    if (e.status === 415 || e.status === 400) return "That isn't a readable EPUB or PDF.";
    if (e.status === 429) return "Your library is full (50-book limit).";
    if (e.status === 401) return "Please sign in again.";
  }
  return "Upload failed — please try again.";
}

export default function UploadBook({ onUploadsChange, onReady }: UploadBookProps) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  const patch = useCallback(
    (key: string, next: Partial<UploadItem>) => {
      setItems((prev) => {
        const updated = prev.map((it) => (it.key === key ? { ...it, ...next } : it));
        onUploadsChange?.(updated);
        return updated;
      });
    },
    [onUploadsChange],
  );

  const ingest = useCallback(
    async (file: File) => {
      const key = `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const title = file.name.replace(/\.(epub|pdf)$/i, "");
      const start: UploadItem = { key, title, status: "uploading", progress: 0 };
      setItems((prev) => {
        const next = [start, ...prev];
        onUploadsChange?.(next);
        return next;
      });
      try {
        const created = await uploadBook(file);
        patch(key, { status: "importing", book: created, title: created.title, stage: "importing" });
        const ready = await pollBookUntilReady(created.id, (b) =>
          patch(key, {
            book: b,
            progress: b.progress,
            stage: b.isNew ? "importing" : "ready",
          }),
        );
        if (ready.isNew) {
          patch(key, { status: "importing", stage: "still importing — large book" });
        } else {
          patch(key, { status: "ready", progress: 100, stage: "ready", book: ready });
          announce(`${ready.title} is ready in your library`, "polite");
          onReady?.(ready);
        }
      } catch (e) {
        const msg = friendlyError(e);
        patch(key, { status: "error", error: msg });
        announce(`Upload of ${title} failed: ${msg}`, "assertive");
      }
    },
    [onReady, onUploadsChange, patch],
  );

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      for (const file of Array.from(files)) {
        const err = validate(file);
        if (err) {
          announce(`Can't add ${file.name}: ${err}`, "assertive");
          const key = `${file.name}-${Date.now()}-err`;
          setItems((prev) => {
            const next = [{ key, title: file.name, status: "error" as const, progress: 0, error: err }, ...prev];
            onUploadsChange?.(next);
            return next;
          });
          continue;
        }
        void ingest(file);
      }
    },
    [ingest, onUploadsChange],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      handleFiles(e.dataTransfer.files);
    },
    [handleFiles],
  );

  const dismiss = useCallback(
    (key: string) => {
      setItems((prev) => {
        const next = prev.filter((it) => it.key !== key);
        onUploadsChange?.(next);
        return next;
      });
    },
    [onUploadsChange],
  );

  return (
    <div className="mb-8">
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        aria-label="Upload a book — drag an EPUB or PDF here, or click to choose a file"
        className="w-full rounded-lg px-5 py-4 flex items-center gap-3.5 text-left transition-colors"
        style={{
          border: `1px dashed ${dragging ? "rgba(212,164,78,0.8)" : "rgba(255,255,255,0.18)"}`,
          background: dragging ? "rgba(212,164,78,0.10)" : "rgba(255,255,255,0.04)",
        }}
      >
        <span
          aria-hidden
          className="flex items-center justify-center rounded-md shrink-0"
          style={{ width: 40, height: 40, background: "rgba(212,164,78,0.16)" }}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgb(212,164,78)" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 16V4M7 9l5-5 5 5" />
            <path d="M5 16v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2" />
          </svg>
        </span>
        <span className="flex flex-col">
          <span className="text-sm font-medium text-kinora-text">
            {dragging ? "Drop to add to your library" : "Upload your own book"}
          </span>
          <span className="text-[11px] text-kinora-muted">
            Drag an EPUB or PDF here, or click to choose · up to 1GB, large books OK
          </span>
        </span>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept=".epub,.pdf,.txt,.mobi,.azw3,.html,.htm,.docx,application/epub+zip,application/pdf,text/plain,text/html"
          multiple
          className="hidden"
          onChange={(e) => {
            handleFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </button>

      {items.length > 0 && (
        <ul className="mt-3 flex flex-col gap-2" aria-label="Uploads in progress">
          {items.map((it) => (
            <li
              key={it.key}
              className="flex items-center gap-3 rounded-md px-3 py-2 text-[12px]"
              style={{ background: "rgba(255,255,255,0.05)", border: "0.5px solid rgba(255,255,255,0.10)" }}
            >
              <span
                aria-hidden
                className="inline-flex h-2 w-2 rounded-full shrink-0"
                style={{
                  background:
                    it.status === "ready" ? "#34d399" : it.status === "error" ? "#f87171" : "#d4a44e",
                  boxShadow: it.status === "importing" ? "0 0 6px #d4a44e" : undefined,
                }}
              />
              <span className="flex-1 truncate text-kinora-text">{it.title}</span>
              <span className="text-kinora-muted">
                {it.status === "uploading" && "Uploading…"}
                {it.status === "importing" && (it.stage ?? "Importing…")}
                {it.status === "ready" && "Ready ✓"}
                {it.status === "error" && (it.error ?? "Failed")}
              </span>
              {(it.status === "ready" || it.status === "error") && (
                <button
                  type="button"
                  onClick={() => dismiss(it.key)}
                  aria-label={`Dismiss ${it.title}`}
                  className="text-kinora-muted hover:text-kinora-text px-1"
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
