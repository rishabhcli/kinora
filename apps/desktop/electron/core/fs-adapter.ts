/**
 * A `node:fs`-backed {@link AtomicFile} for the config store. No Electron import,
 * so it can be exercised against a real temp dir in tests. Writes go to a
 * sibling `.tmp` then `rename` over the target — atomic on POSIX and good
 * enough on Windows (rename replaces).
 */
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { AtomicFile } from "./config-store.js";

export function createFileAdapter(filePath: string): AtomicFile {
  const dir = dirname(filePath);
  return {
    readText() {
      try {
        return readFileSync(filePath, "utf8");
      } catch {
        return null;
      }
    },
    writeText(text: string) {
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
      const tmp = `${filePath}.${process.pid}.tmp`;
      writeFileSync(tmp, text, { encoding: "utf8", mode: 0o600 });
      renameSync(tmp, filePath);
    },
    quarantine(text: string) {
      try {
        if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
        const bad = join(dir, `corrupt-${Date.now()}.json`);
        writeFileSync(bad, text, { encoding: "utf8", mode: 0o600 });
      } catch {
        /* best effort */
      }
    },
  };
}
