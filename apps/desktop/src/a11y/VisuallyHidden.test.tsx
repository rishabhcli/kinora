import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { VisuallyHidden } from "./VisuallyHidden";

describe("VisuallyHidden", () => {
  it("renders its children so a screen reader can read them", () => {
    render(<VisuallyHidden>loading library</VisuallyHidden>);
    expect(screen.getByText("loading library")).toBeInTheDocument();
  });

  it("applies the sr-only class", () => {
    render(<VisuallyHidden>x</VisuallyHidden>);
    expect(screen.getByText("x")).toHaveClass("sr-only");
  });

  it("merges a caller-supplied className", () => {
    render(<VisuallyHidden className="extra">x</VisuallyHidden>);
    const el = screen.getByText("x");
    expect(el).toHaveClass("sr-only");
    expect(el).toHaveClass("extra");
  });

  it("renders the element named by `as`", () => {
    render(<VisuallyHidden as="div">x</VisuallyHidden>);
    expect(screen.getByText("x").tagName).toBe("DIV");
  });
});
