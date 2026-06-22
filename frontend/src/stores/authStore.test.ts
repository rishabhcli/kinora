import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    auth: { login: vi.fn(), register: vi.fn(), me: vi.fn() },
  };
});

import { ApiError, auth } from "../api/client";
import { getToken, setToken } from "../api/token";
import { useAuthStore } from "./authStore";

const authMock = auth as unknown as {
  login: ReturnType<typeof vi.fn>;
  register: ReturnType<typeof vi.fn>;
  me: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  setToken(null);
  useAuthStore.setState({ token: null, user: null, status: "anonymous", error: null });
  vi.clearAllMocks();
});

describe("authStore", () => {
  it("logs in, stores the user, and persists the JWT", async () => {
    authMock.login.mockResolvedValue({ access_token: "tok" });
    authMock.me.mockResolvedValue({ id: "u1", email: "a@b.c" });
    await useAuthStore.getState().login({ email: "a@b.c", password: "secret" });
    const s = useAuthStore.getState();
    expect(s.status).toBe("authenticated");
    expect(s.token).toBe("tok");
    expect(s.user?.email).toBe("a@b.c");
    expect(getToken()).toBe("tok");
  });

  it("surfaces a friendly error and clears the token on bad credentials", async () => {
    authMock.login.mockRejectedValue(new ApiError(401, "unauthorized"));
    await expect(
      useAuthStore.getState().login({ email: "a@b.c", password: "x" }),
    ).rejects.toBeTruthy();
    const s = useAuthStore.getState();
    expect(s.status).toBe("anonymous");
    expect(s.error).toMatch(/incorrect/i);
    expect(getToken()).toBeNull();
  });

  it("registers then auto-logs-in", async () => {
    authMock.register.mockResolvedValue({ id: "u2", email: "n@b.c" });
    authMock.login.mockResolvedValue({ access_token: "tok2" });
    authMock.me.mockResolvedValue({ id: "u2", email: "n@b.c" });
    await useAuthStore.getState().register({ email: "n@b.c", password: "secret" });
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(authMock.register).toHaveBeenCalledTimes(1);
    expect(authMock.login).toHaveBeenCalledTimes(1);
  });

  it("logs out and clears the persisted token", () => {
    setToken("tok");
    useAuthStore.setState({ token: "tok", status: "authenticated", user: { id: "u", email: "e" } });
    useAuthStore.getState().logout();
    expect(useAuthStore.getState().status).toBe("anonymous");
    expect(useAuthStore.getState().user).toBeNull();
    expect(getToken()).toBeNull();
  });

  it("bootstraps an authenticated session from a persisted token", async () => {
    setToken("tok");
    authMock.me.mockResolvedValue({ id: "u1", email: "a@b.c" });
    await useAuthStore.getState().bootstrap();
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(useAuthStore.getState().user?.email).toBe("a@b.c");
  });

  it("clears an invalid token on bootstrap", async () => {
    setToken("bad");
    authMock.me.mockRejectedValue(new ApiError(401, "nope"));
    await useAuthStore.getState().bootstrap();
    expect(useAuthStore.getState().status).toBe("anonymous");
    expect(getToken()).toBeNull();
  });
});
