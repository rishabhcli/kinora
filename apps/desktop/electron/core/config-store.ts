/**
 * A tiny atomic JSON config store for the main process (window state, prefs,
 * update channel, …).
 *
 * The persistence I/O is injected (an {@link AtomicFile}) so the store's logic —
 * defaulting, get/set/delete, debounced flush, corruption recovery — is fully
 * unit-testable with an in-memory file. main.ts wires the real fs adapter
 * pointing at `app.getPath("userData")`.
 *
 * Writes are atomic (write tmp → rename) to survive a crash mid-write, and a
 * corrupt file is quarantined + reset rather than crashing the app.
 */

/** Minimal synchronous file port. The real adapter uses `node:fs`. */
export interface AtomicFile {
  readText(): string | null;
  /** Write atomically (tmp + rename). */
  writeText(text: string): void;
  /** Move a corrupt file aside for forensics; best-effort. */
  quarantine?(text: string): void;
}

export interface ConfigStoreOptions<T extends object> {
  file: AtomicFile;
  defaults: T;
  /** Schema version; a mismatch triggers `migrate`. */
  version?: number;
  /** Migrate persisted data from an older version to the current shape. */
  migrate?: (data: unknown, fromVersion: number) => Partial<T>;
  /** Reject a fully-invalid persisted blob (returns sanitised data or null). */
  validate?: (data: unknown) => Partial<T> | null;
  onLog?: (level: "warn" | "error", message: string, data?: Record<string, unknown>) => void;
}

interface Envelope<T> {
  __kinora: true;
  version: number;
  data: T;
}

export class ConfigStore<T extends object> {
  private readonly file: AtomicFile;
  private readonly defaults: T;
  private readonly version: number;
  private readonly migrate?: ConfigStoreOptions<T>["migrate"];
  private readonly validate?: ConfigStoreOptions<T>["validate"];
  private readonly onLog: NonNullable<ConfigStoreOptions<T>["onLog"]>;
  private cache: T;
  private dirty = false;

  constructor(opts: ConfigStoreOptions<T>) {
    this.file = opts.file;
    this.defaults = opts.defaults;
    this.version = opts.version ?? 1;
    this.migrate = opts.migrate;
    this.validate = opts.validate;
    this.onLog = opts.onLog ?? (() => {});
    this.cache = this.load();
  }

  get<K extends keyof T>(key: K): T[K] {
    return this.cache[key];
  }

  /** Shallow snapshot of the whole store (defensive copy). */
  all(): T {
    return { ...this.cache };
  }

  set<K extends keyof T>(key: K, value: T[K]): void {
    if (Object.is(this.cache[key], value)) return;
    this.cache[key] = value;
    this.dirty = true;
    this.persist();
  }

  /** Merge a partial patch in one write. */
  merge(patch: Partial<T>): void {
    let changed = false;
    for (const [k, v] of Object.entries(patch)) {
      if (!Object.is((this.cache as Record<string, unknown>)[k], v)) {
        (this.cache as Record<string, unknown>)[k] = v;
        changed = true;
      }
    }
    if (changed) {
      this.dirty = true;
      this.persist();
    }
  }

  delete<K extends keyof T>(key: K): void {
    if (!(key in this.cache)) return;
    delete this.cache[key];
    this.dirty = true;
    this.persist();
  }

  /** Reset to defaults and persist. */
  reset(): void {
    this.cache = structuredClone(this.defaults);
    this.dirty = true;
    this.persist();
  }

  /** Force-flush any pending write (e.g. on `before-quit`). */
  flush(): void {
    if (!this.dirty) return;
    this.persist();
  }

  private persist(): void {
    const env: Envelope<T> = { __kinora: true, version: this.version, data: this.cache };
    try {
      this.file.writeText(JSON.stringify(env, null, 2));
      this.dirty = false;
    } catch (err) {
      this.onLog("error", "config-store: write failed", {
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private load(): T {
    const text = safeRead(this.file);
    if (text == null) return structuredClone(this.defaults);

    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      this.onLog("warn", "config-store: corrupt JSON, resetting");
      this.file.quarantine?.(text);
      return structuredClone(this.defaults);
    }

    const env = parsed as Partial<Envelope<unknown>>;
    let data: unknown = env && env.__kinora ? env.data : parsed;
    const fromVersion = typeof env?.version === "number" ? env.version : 0;

    if (this.migrate && fromVersion !== this.version) {
      try {
        data = { ...(data as object), ...this.migrate(data, fromVersion) };
      } catch (err) {
        this.onLog("warn", "config-store: migration failed", {
          message: err instanceof Error ? err.message : String(err),
        });
      }
    }

    if (this.validate) {
      const ok = this.validate(data);
      if (!ok) {
        this.onLog("warn", "config-store: failed validation, resetting");
        return structuredClone(this.defaults);
      }
      data = ok;
    }

    // Merge over defaults so newly-added keys always exist.
    return { ...structuredClone(this.defaults), ...(data as Partial<T>) };
  }
}

function safeRead(file: AtomicFile): string | null {
  try {
    return file.readText();
  } catch {
    return null;
  }
}
