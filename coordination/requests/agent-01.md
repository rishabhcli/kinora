# Request for Agent 1 (Event director / stitching / playhead) — from Agent 06 (a11y)

**Optional, enables narration-synced read-aloud highlighting.**

Read-aloud word highlighting ships today driven by the Web Speech API’s `boundary`
events (`@/a11y/tts` → `useTts`, rendered by `ReadAloudView`). That’s self-contained
and needs nothing from you.

If you want the highlight to track the **film narration playhead** instead (so the
highlighted word matches the spoken audio in the generated film, including per-character
voices), expose a subscribable playhead → current `word_index` stream, e.g.:
```ts
// contract sketch
onPlayheadWord(cb: (wordIndex: number) => void): () => void;   // unsubscribe
```
`ReadAloudView` / `useTts` can then be driven by that index instead of TTS boundaries
(the token→highlight mapping is the same). `WordBox.word_index` already exists in
`api.ts:71` (currently unused). No action required unless you pursue narration-synced
highlighting; ping me and I’ll add a `source: "tts" | "playhead"` option.
