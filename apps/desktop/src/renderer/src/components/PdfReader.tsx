import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";

interface WordBox {
  word_index?: number;
  bbox?: number[];
  text?: string;
}

interface PdfReaderProps {
  bookId: string;
  page: number;
  highlightWordIndex: number | null;
  onSeekWord: (word: number) => void;
}

/**
 * The page pane: the rendered page image with a per-word overlay. The word
 * matching the playhead's `highlightWordIndex` is painted (karaoke); clicking a
 * word seeks the playhead there. Boxes are normalized [x, y, w, h] (§9.4).
 */
export function PdfReader({ bookId, page, highlightWordIndex, onSeekWord }: PdfReaderProps) {
  const { data } = useQuery({
    queryKey: queryKeys.page(bookId, page),
    enabled: Boolean(bookId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: page } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });

  const boxes = (data?.word_boxes ?? []) as WordBox[];

  return (
    <div className="h-full overflow-auto bg-neutral-900">
      {data?.image_url ? (
        <div className="relative mx-auto w-full max-w-3xl">
          <img src={data.image_url} alt={`page ${page}`} className="block w-full select-none" />
          {boxes.map((box) => {
            const b = box.bbox;
            const index = box.word_index;
            if (index === undefined || !b || b.length !== 4) return null;
            const [x, y, w, h] = b;
            if (x === undefined || y === undefined || w === undefined || h === undefined) return null;
            const active = index === highlightWordIndex;
            return (
              <button
                key={index}
                type="button"
                onClick={() => onSeekWord(index)}
                aria-label={box.text}
                className={
                  active
                    ? "absolute rounded-sm bg-amber-300/40"
                    : "absolute rounded-sm hover:bg-white/10"
                }
                style={{
                  left: `${x * 100}%`,
                  top: `${y * 100}%`,
                  width: `${w * 100}%`,
                  height: `${h * 100}%`,
                }}
              />
            );
          })}
        </div>
      ) : (
        <p className="p-6 text-sm text-neutral-500">Loading page…</p>
      )}
    </div>
  );
}
