import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RegionCommentBar from "./RegionCommentBar";

// Mock the director client so we can assert the REST regen path is taken.
const comment = vi.fn();
vi.mock("../../lib/api/director", () => ({
  director: {
    comment: (...args: unknown[]) => comment(...args),
  },
}));

beforeEach(() => {
  comment.mockReset();
});

describe("RegionCommentBar", () => {
  it("is disabled with no session and explains why", () => {
    render(<RegionCommentBar sessionId={null} shotId="s1" />);
    expect(screen.getByText(/start a session to direct/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /re-render/i })).toBeDisabled();
  });

  it("POSTs the note via director.comment (the REST regen endpoint) and shows routing", async () => {
    comment.mockResolvedValue({
      shot_id: "s1",
      agent: "cinematographer",
      aspect: "pacing",
      message: "Slowing the cut.",
      job_id: "job-12345678",
      learned: [],
    });
    const onCommented = vi.fn();
    render(<RegionCommentBar sessionId="sess1" shotId="s1" onCommented={onCommented} />);

    fireEvent.change(screen.getByPlaceholderText(/direct this shot/i), {
      target: { value: "slower please" },
    });
    fireEvent.click(screen.getByRole("button", { name: /re-render/i }));

    await waitFor(() => expect(comment).toHaveBeenCalledWith("sess1", { shot_id: "s1", note: "slower please" }));
    expect(await screen.findByText(/Cinematographer/)).toBeInTheDocument();
    // job id is shown truncated to its first 8 chars ("job-12345678" -> "job-1234")
    expect(screen.getByText(/job job-1234/i)).toBeInTheDocument();
    expect(onCommented).toHaveBeenCalled();
  });

  it("submits a preset chip directly", async () => {
    comment.mockResolvedValue({ shot_id: "s1", agent: "continuity", aspect: "canon", message: "ok", job_id: null, learned: [] });
    render(<RegionCommentBar sessionId="sess1" shotId="s1" presets={["Warmer light"]} />);
    fireEvent.click(screen.getByRole("button", { name: "Warmer light" }));
    await waitFor(() => expect(comment).toHaveBeenCalledWith("sess1", { shot_id: "s1", note: "Warmer light" }));
  });

  it("surfaces a learned-prior confirmation", async () => {
    comment.mockResolvedValue({
      shot_id: "s1",
      agent: "cinematographer",
      aspect: "pacing",
      message: "ok",
      job_id: null,
      learned: [{ kind: "pace", bias: -0.3, weight: 1, label: "Slower shots", detail: "", applied: true, applied_value: "slow", last_note: null }],
    });
    render(<RegionCommentBar sessionId="sess1" shotId="s1" />);
    fireEvent.change(screen.getByPlaceholderText(/direct this shot/i), { target: { value: "slower" } });
    fireEvent.click(screen.getByRole("button", { name: /re-render/i }));
    expect(await screen.findByText(/will be the default/i)).toBeInTheDocument();
  });
});
