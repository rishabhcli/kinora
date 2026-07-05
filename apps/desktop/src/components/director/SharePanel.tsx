// SharePanel — sharing + export for the Director's work on a book. Produces a
// `kinora://` deep link to the book (copy to clipboard), exports the canon as
// markdown, exports annotations as a portable bundle, and exports the whole
// "director project" (canon + annotations + collections) as one JSON file.
// Downloads use a Blob + anchor (works in the Electron renderer + browser);
// clipboard uses navigator.clipboard with a textarea fallback.
import { useCallback, useState } from "react";
import type { Book } from "../../data/books";
import {
  encodeShareLink,
  canonToMarkdown,
  buildProjectExport,
  serializeExport,
  exportFilename,
} from "../../lib/api/sharing";
import type { CanonGraph } from "../../lib/api/director";
import type { AnnotationStore } from "../../lib/api/annotations";
import type { SmartCollection } from "../../lib/api/collections";

interface SharePanelProps {
  book: Book;
  canon: CanonGraph | null;
  annotations: AnnotationStore;
  collections?: SmartCollection[];
}

/** Trigger a client download of `text` as `filename`. */
function download(filename: string, text: string, mime = "application/json"): void {
  try {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch {
    /* download blocked — no-op */
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

function Row({ title, desc, action }: { title: string; desc: string; action: React.ReactNode }) {
  return (
    <div
      className="flex items-center justify-between gap-3 rounded-xl px-3.5 py-3"
      style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.07)" }}
    >
      <div className="min-w-0">
        <p className="text-[12px] font-medium text-kinora-text">{title}</p>
        <p className="text-[10.5px] text-kinora-muted">{desc}</p>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}

function ActionButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg px-3 py-1.5 text-[10.5px] font-semibold transition-all"
      style={{ background: "rgba(212,164,78,0.16)", color: "rgba(236,231,223,0.95)", border: "1px solid rgba(212,164,78,0.28)" }}
    >
      {label}
    </button>
  );
}

export default function SharePanel({ book, canon, annotations, collections = [] }: SharePanelProps) {
  const [copied, setCopied] = useState(false);

  const shareLink = encodeShareLink({ kind: "book", book_id: book.id });

  const onCopyLink = useCallback(async () => {
    const ok = await copyText(shareLink);
    setCopied(ok);
    if (ok) setTimeout(() => setCopied(false), 2000);
  }, [shareLink]);

  const onExportCanon = useCallback(() => {
    if (!canon) return;
    download(exportFilename("canon", book.title, "md"), canonToMarkdown(canon), "text/markdown");
  }, [canon, book.title]);

  const onExportAnnotations = useCallback(() => {
    const bundle = annotations.exportBook(book.id);
    download(exportFilename("notes", book.title), serializeExport(bundle));
  }, [annotations, book.id, book.title]);

  const onExportProject = useCallback(() => {
    const bundle = buildProjectExport(book.id, {
      canon: canon ?? undefined,
      annotations: annotations.exportBook(book.id),
      collections,
    });
    download(exportFilename("kinora-project", book.title), serializeExport(bundle));
  }, [book.id, book.title, canon, annotations, collections]);

  return (
    <div className="flex flex-col gap-3">
      <Row
        title="Share link"
        desc={copied ? "Copied to clipboard" : shareLink}
        action={<ActionButton label={copied ? "Copied ✓" : "Copy"} onClick={() => void onCopyLink()} />}
      />
      <Row
        title="Export canon (Markdown)"
        desc="The canon brief as a readable document"
        action={<ActionButton label="Download" onClick={onExportCanon} />}
      />
      <Row
        title="Export annotations"
        desc="A portable bundle of every note + thread"
        action={<ActionButton label="Download" onClick={onExportAnnotations} />}
      />
      <Row
        title="Export project"
        desc="Canon + annotations + collections in one file"
        action={<ActionButton label="Download" onClick={onExportProject} />}
      />
    </div>
  );
}
