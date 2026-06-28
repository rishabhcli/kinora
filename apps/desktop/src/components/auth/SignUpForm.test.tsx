import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SignUpForm from "./SignUpForm";

function fill(email: string, pw: string, confirm: string) {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: pw } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: confirm } });
}

describe("SignUpForm", () => {
  it("rejects a weak/short password", () => {
    const onSubmit = vi.fn();
    render(<SignUpForm onSubmit={onSubmit} />);
    fill("a@x.com", "short", "short");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("requires matching confirmation", () => {
    const onSubmit = vi.fn();
    render(<SignUpForm onSubmit={onSubmit} />);
    fill("a@x.com", "Str0ng&Pass", "different");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits a strong, matching password", () => {
    const onSubmit = vi.fn();
    render(<SignUpForm onSubmit={onSubmit} />);
    fill("a@x.com", "Str0ng&Pass", "Str0ng&Pass");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(onSubmit).toHaveBeenCalledWith("a@x.com", "Str0ng&Pass");
  });

  it("shows the requirements checklist once the password is touched", () => {
    render(<SignUpForm onSubmit={vi.fn()} />);
    const pw = screen.getByLabelText(/^password$/i);
    fireEvent.change(pw, { target: { value: "abc" } });
    fireEvent.blur(pw);
    expect(screen.getByLabelText(/password requirements/i)).toBeInTheDocument();
  });
});
