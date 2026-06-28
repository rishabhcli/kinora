import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import OnboardingFlow from "./OnboardingFlow";
import { ONBOARDING_STORAGE_KEY } from "../../lib/account";

beforeEach(() => {
  try {
    localStorage.removeItem(ONBOARDING_STORAGE_KEY);
  } catch {
    /* ignore */
  }
});

describe("OnboardingFlow", () => {
  it("starts at the welcome step", () => {
    render(<OnboardingFlow onFinish={vi.fn()} />);
    expect(screen.getByRole("dialog", { name: /welcome to kinora/i })).toBeInTheDocument();
    expect(screen.getByText(/Welcome to Kinora/i)).toBeInTheDocument();
    expect(screen.getByText(/Step 1 of 6/i)).toBeInTheDocument();
  });

  it("advances through steps with Continue and shows Back after the first", () => {
    render(<OnboardingFlow onFinish={vi.fn()} email="reader@x.com" />);
    expect(screen.queryByRole("button", { name: /^back$/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    // now on the profile step
    expect(screen.getByText(/Make it yours/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^back$/i })).toBeInTheDocument();
  });

  it("can skip the whole flow", () => {
    const onFinish = vi.fn();
    render(<OnboardingFlow onFinish={onFinish} />);
    fireEvent.click(screen.getByRole("button", { name: /skip setup/i }));
    expect(onFinish).toHaveBeenCalled();
  });

  it("persists progress so it resumes on remount", () => {
    const { unmount } = render(<OnboardingFlow onFinish={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /continue/i })); // → profile
    unmount();
    render(<OnboardingFlow onFinish={vi.fn()} />);
    expect(screen.getByText(/Make it yours/i)).toBeInTheDocument();
  });

  it("optional steps offer a 'Skip this' affordance", () => {
    render(<OnboardingFlow onFinish={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /continue/i })); // → profile (required)
    expect(screen.queryByRole("button", { name: /skip this/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /continue/i })); // → taste (optional)
    expect(screen.getByRole("button", { name: /skip this/i })).toBeInTheDocument();
  });
});
