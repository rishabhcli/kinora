import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Avatar, Toggle, Segmented } from "./primitives";

describe("Avatar", () => {
  it("renders initials when no image", () => {
    render(<Avatar profile={{ id: "u1", displayName: "Ada Lovelace", email: "a@x.com" }} />);
    expect(screen.getByText("AL")).toBeInTheDocument();
  });
  it("renders an image when avatarUrl is set", () => {
    const { container } = render(
      <Avatar profile={{ id: "u1", displayName: "Ada", email: "a@x.com", avatarUrl: "/a.png" }} />,
    );
    expect(container.querySelector("img")).toHaveAttribute("src", "/a.png");
  });
});

describe("Toggle", () => {
  it("is a switch that reflects + flips state", () => {
    const onChange = vi.fn();
    const { rerender } = render(<Toggle label="X" checked={false} onChange={onChange} />);
    const sw = screen.getByRole("switch", { name: "X" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
    rerender(<Toggle label="X" checked onChange={onChange} />);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });
});

describe("Segmented", () => {
  it("marks the selected tab and reports changes", () => {
    const onChange = vi.fn();
    render(
      <Segmented
        value="b"
        onChange={onChange}
        options={[
          { value: "a", label: "A" },
          { value: "b", label: "B" },
        ]}
      />,
    );
    expect(screen.getByRole("tab", { name: "B" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "A" }));
    expect(onChange).toHaveBeenCalledWith("a");
  });
});
