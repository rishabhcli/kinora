import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.fn();
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, http: (...args: unknown[]) => httpMock(...args) };
});

import { ApiError } from "../api";
import {
  listSessions,
  revokeSession,
  revokeOtherSessions,
  listPasskeys,
  renamePasskey,
  removePasskey,
  finishPasskeyRegistration,
} from "./sessions";

beforeEach(() => httpMock.mockReset());

describe("listSessions", () => {
  it("parses + sorts backend rows", async () => {
    httpMock.mockResolvedValueOnce([
      { id: "a", kind: "web", created_at: 1, last_seen_at: 5 },
      { bad: 1 },
    ]);
    const list = await listSessions();
    expect(list.map((s) => s.id)).toEqual(["a"]);
  });
  it("returns [] when the endpoint is missing", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await listSessions()).toEqual([]);
  });
  it("rethrows a 401 (must re-auth)", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(401, "x"));
    await expect(listSessions()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("revoke", () => {
  it("DELETEs a single session with an encoded id", async () => {
    httpMock.mockResolvedValueOnce(null);
    expect(await revokeSession("a b/c")).toBe(true);
    expect(httpMock).toHaveBeenCalledWith("/api/auth/sessions/a%20b%2Fc", { method: "DELETE" });
  });
  it("DELETEs all others", async () => {
    httpMock.mockResolvedValueOnce(null);
    expect(await revokeOtherSessions()).toBe(true);
    expect(httpMock).toHaveBeenCalledWith("/api/auth/sessions", { method: "DELETE" });
  });
  it("degrades to true when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await revokeSession("a")).toBe(true);
  });
});

describe("passkeys", () => {
  it("lists, renames, removes", async () => {
    httpMock.mockResolvedValueOnce([{ id: "k1", label: "Mac", kind: "platform" }]);
    expect((await listPasskeys()).map((c) => c.id)).toEqual(["k1"]);

    httpMock.mockResolvedValueOnce(null);
    expect(await renamePasskey("k1", "Work")).toBe(true);
    expect(httpMock).toHaveBeenLastCalledWith("/api/auth/passkeys/k1", {
      method: "PATCH",
      body: JSON.stringify({ label: "Work" }),
    });

    httpMock.mockResolvedValueOnce(null);
    expect(await removePasskey("k1")).toBe(true);
  });

  it("finishPasskeyRegistration returns the stored credential", async () => {
    httpMock.mockResolvedValueOnce({ id: "k2", label: "Key", kind: "cross-platform" });
    const c = await finishPasskeyRegistration({ rawId: "x" });
    expect(c?.id).toBe("k2");
  });

  it("finishPasskeyRegistration returns null when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(501, "soon"));
    expect(await finishPasskeyRegistration({})).toBeNull();
  });
});
