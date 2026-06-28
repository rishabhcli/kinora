import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  webauthnAvailable,
  toBase64Url,
  fromBase64Url,
  parsePasskey,
  parsePasskeys,
  sortPasskeys,
  suggestPasskeyLabel,
  createPasskeyRegistry,
  type PasskeyCredential,
} from "./passkey";

function cred(over: Partial<PasskeyCredential> = {}): PasskeyCredential {
  return {
    id: over.id ?? "c1",
    label: over.label ?? "Passkey",
    kind: over.kind ?? "platform",
    createdAt: over.createdAt ?? 1000,
    ...over,
  };
}

describe("webauthnAvailable", () => {
  it("is false without a navigator", () => {
    expect(webauthnAvailable(undefined)).toBe(false);
  });
  it("depends on credentials + PublicKeyCredential", () => {
    const nav = { credentials: {} } as unknown as Navigator;
    const hadPkc = "PublicKeyCredential" in globalThis;
    (globalThis as Record<string, unknown>).PublicKeyCredential = function () {};
    expect(webauthnAvailable(nav)).toBe(true);
    if (!hadPkc) delete (globalThis as Record<string, unknown>).PublicKeyCredential;
  });
});

describe("base64url codec", () => {
  it("round-trips arbitrary bytes", () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 251, 252, 253, 254, 255]);
    const enc = toBase64Url(bytes);
    expect(enc).not.toMatch(/[+/=]/); // url-safe, unpadded
    expect([...fromBase64Url(enc)]).toEqual([...bytes]);
  });
  it("decodes a known value", () => {
    // "Man" → "TWFu"
    expect(toBase64Url(new Uint8Array([77, 97, 110]))).toBe("TWFu");
  });
});

describe("parsePasskey", () => {
  it("requires an id, maps snake_case", () => {
    expect(parsePasskey({ label: "x" })).toBeNull();
    const c = parsePasskey({ id: "k", last_used_at: 5000, this_device: true, kind: "cross-platform" })!;
    expect(c).toMatchObject({ id: "k", lastUsedAt: 5000, thisDevice: true, kind: "cross-platform" });
  });
  it("defaults label and unknown kind", () => {
    const c = parsePasskey({ id: "k", kind: "weird" })!;
    expect(c.label).toBe("Passkey");
    expect(c.kind).toBe("unknown");
  });
  it("parsePasskeys drops malformed rows", () => {
    expect(parsePasskeys([{ id: "a" }, {}, 7]).map((c) => c.id)).toEqual(["a"]);
  });
});

describe("sortPasskeys", () => {
  it("puts this-device first, then most recently used", () => {
    const list = [
      cred({ id: "a", createdAt: 10 }),
      cred({ id: "b", lastUsedAt: 99, createdAt: 1 }),
      cred({ id: "this", thisDevice: true, createdAt: 0 }),
    ];
    expect(sortPasskeys(list).map((c) => c.id)).toEqual(["this", "b", "a"]);
  });
});

describe("suggestPasskeyLabel", () => {
  it("names by platform and kind", () => {
    expect(suggestPasskeyLabel("platform", "macOS")).toBe("Touch ID / Face ID");
    expect(suggestPasskeyLabel("platform", "Windows")).toBe("Windows Hello");
    expect(suggestPasskeyLabel("platform", "Linux")).toBe("This device");
    expect(suggestPasskeyLabel("cross-platform")).toBe("Security key");
    expect(suggestPasskeyLabel("unknown")).toBe("Passkey");
  });
});

describe("createPasskeyRegistry", () => {
  it("adds, renames, removes, persists, notifies", () => {
    const backing = memoryStore();
    const reg = createPasskeyRegistry(backing);
    let hits = 0;
    reg.subscribe(() => hits++);

    reg.add(cred({ id: "k1", label: "Mac" }));
    reg.add(cred({ id: "k2", label: "Key", kind: "cross-platform" }));
    expect(reg.list().map((c) => c.id).sort()).toEqual(["k1", "k2"]);
    expect(hits).toBe(2);

    reg.rename("k1", "  Work Mac  ");
    expect(reg.list().find((c) => c.id === "k1")?.label).toBe("Work Mac");

    // empty rename keeps the old label
    reg.rename("k1", "   ");
    expect(reg.list().find((c) => c.id === "k1")?.label).toBe("Work Mac");

    reg.remove("k2");
    expect(reg.list().map((c) => c.id)).toEqual(["k1"]);

    // rehydrate from backing
    expect(createPasskeyRegistry(backing).list().map((c) => c.id)).toEqual(["k1"]);
  });

  it("add replaces a credential with the same id", () => {
    const reg = createPasskeyRegistry(memoryStore());
    reg.add(cred({ id: "k", label: "Old" }));
    reg.add(cred({ id: "k", label: "New" }));
    expect(reg.list()).toHaveLength(1);
    expect(reg.list()[0].label).toBe("New");
  });
});
