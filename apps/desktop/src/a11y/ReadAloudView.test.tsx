import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ReadAloudView } from "./ReadAloudView";
import { installSpeech, type MockUtterance } from "@/test/mockSpeech";

let speech: ReturnType<typeof installSpeech>;

beforeEach(() => {
  speech = installSpeech();
  // jsdom lacks scrollIntoView
  Element.prototype.scrollIntoView = () => {};
});
afterEach(() => speech.uninstall());

describe("ReadAloudView", () => {
  it("renders the page text as readable words", () => {
    render(<ReadAloudView text="The quick brown fox" />);
    expect(screen.getByTestId("read-aloud-text").textContent).toBe("The quick brown fox");
  });

  it("offers a read-aloud control that starts speech", () => {
    render(<ReadAloudView text="The quick brown fox" />);
    fireEvent.click(screen.getByRole("button", { name: /read aloud/i }));
    expect(speech.synth.speak).toHaveBeenCalledTimes(1);
  });

  it("highlights the spoken word in lockstep with boundary events", () => {
    render(<ReadAloudView text="The quick brown fox" />);
    fireEvent.click(screen.getByRole("button", { name: /read aloud/i }));
    const u = speech.spoken[0] as MockUtterance;
    act(() => u.fire("boundary", { name: "word", charIndex: 4 })); // "quick"
    expect(screen.getByText("quick")).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("The")).not.toHaveAttribute("aria-current", "true");
    act(() => u.fire("boundary", { name: "word", charIndex: 16 })); // "fox"
    expect(screen.getByText("fox")).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("quick")).not.toHaveAttribute("aria-current", "true");
  });

  it("clears the highlight and returns to idle when speech ends", () => {
    render(<ReadAloudView text="a b" />);
    fireEvent.click(screen.getByRole("button", { name: /read aloud/i }));
    const u = speech.spoken[0] as MockUtterance;
    act(() => u.fire("boundary", { name: "word", charIndex: 0 }));
    expect(screen.getByText("a")).toHaveAttribute("aria-current", "true");
    act(() => u.fire("end", {}));
    expect(screen.getByText("a")).not.toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /read aloud/i })).toBeInTheDocument();
  });

  it("falls back to a disabled note when the Web Speech API is unavailable", () => {
    speech.uninstall();
    render(<ReadAloudView text="hi" />);
    expect(screen.getByRole("button", { name: /read aloud/i })).toBeDisabled();
  });
});
