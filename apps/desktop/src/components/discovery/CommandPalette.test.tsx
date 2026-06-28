import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import CommandPalette from "./CommandPalette";
import type { Command } from "../../lib/discovery/palette";

function cmd(over: Partial<Command> & Pick<Command, "id" | "title">): Command {
  return { group: "action", run: () => {}, ...over };
}

function commands(runs: Record<string, () => void> = {}): Command[] {
  return [
    cmd({ id: "home", title: "Go to Home", group: "navigation", run: runs.home }),
    cmd({ id: "library", title: "Go to Library", group: "navigation", run: runs.library }),
    cmd({ id: "dune", title: "Dune", group: "book", keywords: ["Frank Herbert"], run: runs.dune }),
    cmd({ id: "upload", title: "Upload a Book", group: "action", run: runs.upload }),
  ];
}

describe("CommandPalette", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it("renders nothing when closed", () => {
    const { container } = render(<CommandPalette open={false} commands={commands()} onClose={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a dialog with grouped commands when open", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    expect(screen.getByRole("dialog", { name: /command palette/i })).toBeInTheDocument();
    expect(screen.getByText("Go to")).toBeInTheDocument(); // navigation group label
    expect(screen.getByRole("option", { name: /Dune/ })).toBeInTheDocument();
  });

  it("filters commands as you type", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "dune" } });
    expect(screen.getByRole("option", { name: /Dune/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Upload/ })).not.toBeInTheDocument();
  });

  it("matches commands via keywords", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "herbert" } });
    expect(screen.getByRole("option", { name: /Dune/ })).toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "zzzz" } });
    expect(screen.getByText(/No results/)).toBeInTheDocument();
  });

  it("runs the highlighted command on Enter and closes", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const onClose = vi.fn();
    render(<CommandPalette open commands={commands({ dune: run })} onClose={onClose} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "dune" } });
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Enter" });
    expect(onClose).toHaveBeenCalled();
    vi.runAllTimers();
    expect(run).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("moves the selection with arrow keys", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    const dialog = screen.getByRole("dialog");
    // first option selected by default
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
  });

  it("wraps selection past the ends", () => {
    render(<CommandPalette open commands={commands()} onClose={() => {}} />);
    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(dialog, { key: "ArrowUp" }); // wrap to last
    const options = screen.getAllByRole("option");
    expect(options[options.length - 1]).toHaveAttribute("aria-selected", "true");
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(<CommandPalette open commands={commands()} onClose={onClose} />);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("runs a command on click", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const onClose = vi.fn();
    render(<CommandPalette open commands={commands({ upload: run })} onClose={onClose} />);
    fireEvent.click(screen.getByRole("option", { name: /Upload a Book/ }));
    vi.runAllTimers();
    expect(run).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });
});
