import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import MfaChallenge from "./MfaChallenge";

describe("MfaChallenge", () => {
  it("validates the 6-digit code before submitting", () => {
    const onSubmit = vi.fn();
    render(<MfaChallenge onSubmit={onSubmit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/authentication code/i), { target: { value: "123 456" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));
    expect(onSubmit).toHaveBeenCalledWith("123456"); // normalized
  });

  it("switches to recovery-code mode and validates its shape", () => {
    const onSubmit = vi.fn();
    render(<MfaChallenge onSubmit={onSubmit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /recovery code instead/i }));
    fireEvent.change(screen.getByLabelText(/recovery code/i), { target: { value: "abcd-1234" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));
    expect(onSubmit).toHaveBeenCalledWith("abcd-1234");
  });

  it("cancels back to sign in", () => {
    const onCancel = vi.fn();
    render(<MfaChallenge onSubmit={vi.fn()} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole("button", { name: /back to sign in/i }));
    expect(onCancel).toHaveBeenCalled();
  });
});
