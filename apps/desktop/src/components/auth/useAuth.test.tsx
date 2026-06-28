import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Mock the api surface useAuth composes against. `vi.hoisted` lets the mock
// object exist before the hoisted vi.mock factory runs.
const apiMock = vi.hoisted(() => ({
  isAuthed: vi.fn(() => false),
  loginOrRegister: vi.fn(),
  register: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}));
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: apiMock };
});

import { useAuthController } from "./useAuth";
import { ApiError } from "../../lib/api";

beforeEach(() => {
  vi.useFakeTimers();
  apiMock.isAuthed.mockReturnValue(false);
  apiMock.loginOrRegister.mockReset();
  apiMock.register.mockReset();
  apiMock.login.mockReset();
  apiMock.logout.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("useAuthController", () => {
  it("starts anonymous and authenticates on a successful sign-in", async () => {
    apiMock.loginOrRegister.mockResolvedValue(undefined);
    const { result } = renderHook(() => useAuthController());
    expect(result.current.status).toBe("anonymous");

    let p!: Promise<boolean>;
    act(() => {
      p = result.current.signIn("a@x.com", "pw");
    });
    await act(async () => {
      await p;
    });
    expect(result.current.status).toBe("authenticated");
    expect(apiMock.loginOrRegister).toHaveBeenCalledWith("a@x.com", "pw");
  });

  it("surfaces a friendly error on bad credentials", async () => {
    apiMock.loginOrRegister.mockRejectedValue(new ApiError(401, "incorrect"));
    const { result } = renderHook(() => useAuthController());
    await act(async () => {
      await result.current.signIn("a@x.com", "pw");
    });
    expect(result.current.status).toBe("anonymous");
    expect(result.current.error).toMatch(/incorrect/i);
  });

  it("enters the mfa_required step when the backend asks for a second factor", async () => {
    apiMock.loginOrRegister.mockRejectedValue(new ApiError(401, "mfa required"));
    const { result } = renderHook(() => useAuthController());
    await act(async () => {
      await result.current.signIn("a@x.com", "pw");
    });
    expect(result.current.status).toBe("mfa_required");
  });

  it("submitMfa finishes the challenged sign-in", async () => {
    apiMock.loginOrRegister
      .mockRejectedValueOnce(new ApiError(401, "mfa required"))
      .mockResolvedValueOnce(undefined);
    const { result } = renderHook(() => useAuthController());
    await act(async () => {
      await result.current.signIn("a@x.com", "pw");
    });
    expect(result.current.status).toBe("mfa_required");
    await act(async () => {
      await result.current.submitMfa("123456");
    });
    expect(result.current.status).toBe("authenticated");
  });

  it("times out after the deadline", async () => {
    apiMock.loginOrRegister.mockImplementation(() => new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useAuthController());
    let p!: Promise<boolean>;
    act(() => {
      p = result.current.signIn("a@x.com", "pw");
    });
    await act(async () => {
      vi.advanceTimersByTime(6100);
      await p;
    });
    expect(result.current.status).toBe("anonymous");
    expect(result.current.error).toMatch(/connection|server/i);
  });

  it("signOut clears the token and returns to anonymous", async () => {
    apiMock.loginOrRegister.mockResolvedValue(undefined);
    const { result } = renderHook(() => useAuthController());
    await act(async () => {
      await result.current.signIn("a@x.com", "pw");
    });
    act(() => result.current.signOut());
    expect(apiMock.logout).toHaveBeenCalled();
    expect(result.current.status).toBe("anonymous");
  });

  it("enterDemo authenticates without a backend call", () => {
    const { result } = renderHook(() => useAuthController());
    act(() => result.current.enterDemo());
    expect(result.current.status).toBe("authenticated");
    expect(apiMock.loginOrRegister).not.toHaveBeenCalled();
  });

  it("clears busy after a successful sign-in", async () => {
    apiMock.loginOrRegister.mockResolvedValue(undefined);
    const { result } = renderHook(() => useAuthController());
    await act(async () => {
      await result.current.signIn("a@x.com", "pw");
    });
    expect(result.current.busy).toBe(false);
  });
});
