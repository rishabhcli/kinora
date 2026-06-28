import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SignInForm from "./SignInForm";

describe("SignInForm", () => {
  it("blocks submit + shows errors for invalid input", () => {
    const onSubmit = vi.fn();
    render(<SignInForm onSubmit={onSubmit} />);
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }).closest("form")!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(document.getElementById("signin-email-error")).toHaveTextContent(/email/i);
  });

  it("submits trimmed credentials when valid", () => {
    const onSubmit = vi.fn();
    render(<SignInForm onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "  a@x.com " } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secretpw" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(onSubmit).toHaveBeenCalledWith("a@x.com", "secretpw");
  });

  it("renders a controller error as an alert", () => {
    render(<SignInForm onSubmit={vi.fn()} error="Incorrect email or password." />);
    expect(screen.getByRole("alert")).toHaveTextContent(/incorrect/i);
  });

  it("shows the forgot-password link when provided", () => {
    const onForgot = vi.fn();
    render(<SignInForm onSubmit={vi.fn()} onForgot={onForgot} />);
    fireEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    expect(onForgot).toHaveBeenCalled();
  });
});
