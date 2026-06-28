/**
 * Secure auth-token storage backed by Electron `safeStorage` (Keychain on
 * macOS, DPAPI on Windows, libsecret on Linux).
 *
 * All the framing/validation is in the pure `token-codec`; this service only
 * does the OS-encryption + disk I/O. When `safeStorage.isEncryptionAvailable()`
 * is false (e.g. a headless Linux box with no keyring) we degrade to a marked
 * `plain` envelope rather than refusing to log in — the renderer still works,
 * and the diagnostics panel can surface the weaker posture.
 */
import {
  deobfuscate,
  isPlausibleToken,
  makeEnvelope,
  obfuscate,
  parseEnvelope,
} from "../core/token-codec.js";
import type { AtomicFile } from "../core/config-store.js";
import type { ScopedLogger } from "../core/logger.js";

/**
 * The slice of Electron's `safeStorage` we use, typed structurally so the
 * module imports nothing from `electron` at load time and can be unit-tested in
 * plain Node with a fake. The real one is `require`d lazily in the constructor.
 */
export interface SafeStorageLike {
  isEncryptionAvailable(): boolean;
  encryptString(plain: string): Buffer;
  decryptString(cipher: Buffer): string;
}

export interface SecureStoreDeps {
  file: AtomicFile;
  log: ScopedLogger;
  /** Injectable for tests; defaults to Electron's safeStorage. */
  storage?: SafeStorageLike;
}

export class SecureStore {
  private readonly file: AtomicFile;
  private readonly log: ScopedLogger;
  private readonly storage: SafeStorageLike | null;

  constructor(deps: SecureStoreDeps) {
    this.file = deps.file;
    this.log = deps.log;
    this.storage = deps.storage ?? loadSafeStorage();
  }

  private encAvailable(): boolean {
    try {
      return Boolean(this.storage?.isEncryptionAvailable());
    } catch {
      return false;
    }
  }

  getToken(): string | null {
    const env = parseEnvelope(this.file.readText());
    if (!env) return null;
    if (env.mode === "enc") {
      if (!this.encAvailable()) {
        this.log.warn("secure-store: encrypted token present but encryption unavailable");
        return null;
      }
      try {
        const token = this.storage!.decryptString(Buffer.from(env.payload, "base64"));
        return isPlausibleToken(token) ? token : null;
      } catch (err) {
        this.log.error("secure-store: decrypt failed", { message: msg(err) });
        return null;
      }
    }
    // plain fallback
    const token = deobfuscate(env.payload);
    return token && isPlausibleToken(token) ? token : null;
  }

  setToken(token: string): boolean {
    if (!isPlausibleToken(token)) {
      this.log.warn("secure-store: refused to persist implausible token");
      return false;
    }
    if (this.encAvailable()) {
      try {
        const payload = this.storage!.encryptString(token).toString("base64");
        this.file.writeText(JSON.stringify(makeEnvelope(payload, "enc")));
        return true;
      } catch (err) {
        this.log.error("secure-store: encrypt failed, falling back", { message: msg(err) });
      }
    }
    // Fallback: obfuscated, explicitly marked plain.
    this.log.warn("secure-store: OS encryption unavailable, storing obfuscated token");
    this.file.writeText(JSON.stringify(makeEnvelope(obfuscate(token), "plain")));
    return true;
  }

  clear(): void {
    try {
      this.file.writeText(JSON.stringify(makeEnvelope("", "plain")));
    } catch (err) {
      this.log.error("secure-store: clear failed", { message: msg(err) });
    }
  }

  /** Posture summary for diagnostics. */
  posture(): { encryptionAvailable: boolean } {
    return { encryptionAvailable: this.encAvailable() };
  }
}

function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Lazily resolve Electron's safeStorage; null when outside Electron. */
function loadSafeStorage(): SafeStorageLike | null {
  try {
    const { safeStorage } = require("electron") as { safeStorage?: SafeStorageLike };
    return safeStorage ?? null;
  } catch {
    return null;
  }
}
