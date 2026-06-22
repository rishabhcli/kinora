import { type DragEvent, useRef, useState } from "react";

import { ApiError, books } from "../../api/client";
import type { Book } from "../../api/types";
import { Spinner, UploadIcon } from "../common/icons";

interface UploadDropzoneProps {
  onUploaded: (book: Book) => void;
}

export function UploadDropzone({ onUploaded }: UploadDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setError("Please choose a PDF file.");
      return;
    }
    setError(null);
    setUploading(true);
    setProgress(0);
    try {
      const book = await books.upload(file, (f) => setProgress(f));
      onUploaded(book);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed. Please try again.");
    } finally {
      setUploading(false);
      setProgress(0);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const onDrop = (e: DragEvent<HTMLButtonElement>) => {
    e.preventDefault();
    setDragOver(false);
    void handleFile(e.dataTransfer.files?.[0]);
  };

  return (
    <div>
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        disabled={uploading}
        className={`glass flex w-full flex-col items-center justify-center gap-3 rounded-3xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
          dragOver
            ? "border-kinora-iris/80 bg-kinora-glow/10"
            : "border-kinora-line hover:border-kinora-iris/50"
        } ${uploading ? "cursor-wait" : "cursor-pointer"}`}
      >
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-kinora-glow/15 text-xl text-kinora-iris">
          {uploading ? <Spinner className="h-5 w-5" /> : <UploadIcon className="h-5 w-5" />}
        </span>
        {uploading ? (
          <div className="w-full max-w-xs">
            <p className="text-sm font-medium text-kinora-mist">Uploading…</p>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-kinora-line">
              <div
                className="h-full rounded-full bg-kinora-glow transition-[width] duration-150"
                style={{ width: `${Math.round(progress * 100)}%` }}
              />
            </div>
          </div>
        ) : (
          <div>
            <p className="text-sm font-medium text-kinora-mist">
              Drop a PDF here, or <span className="text-kinora-iris">browse</span>
            </p>
            <p className="mt-1 text-xs text-kinora-muted">
              Importing analyses the book and builds its canon — no video is generated yet.
            </p>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(e) => void handleFile(e.target.files?.[0])}
        />
      </button>
      {error ? (
        <p role="alert" className="mt-2 text-sm text-kinora-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
