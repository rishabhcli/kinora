import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ConflictPanel from "./ConflictPanel";
import type { ConflictRecord } from "../../lib/api/director";

const getConflicts = vi.fn();
const resolveConflict = vi.fn();
vi.mock("../../lib/api/director", () => ({
  director: {
    getConflicts: (...a: unknown[]) => getConflicts(...a),
    resolveConflict: (...a: unknown[]) => resolveConflict(...a),
  },
}));

function conflict(over: Partial<ConflictRecord> = {}): ConflictRecord {
  return {
    conflict_id: "cf_1",
    shot_id: "s1",
    claim: "the heroine draws a sword she lost",
    canon_fact: "sword lost in the river",
    raised_by: "continuity_supervisor",
    current_beat: "beat_0034",
    options: [],
    resolved: false,
    chosen_option: null,
    reasoning: null,
    ...over,
  };
}

beforeEach(() => {
  getConflicts.mockReset();
  resolveConflict.mockReset();
});

describe("ConflictPanel", () => {
  it("prompts to start a session when none is open", () => {
    render(<ConflictPanel sessionId={null} />);
    expect(screen.getByText(/start a session to see and resolve/i)).toBeInTheDocument();
    expect(getConflicts).not.toHaveBeenCalled();
  });

  it("lists conflicts and resolves one via honor_canon", async () => {
    getConflicts.mockResolvedValue([conflict()]);
    resolveConflict.mockResolvedValue({
      conflict_id: "cf_1",
      option: "honor_canon",
      status: "applied",
      shot_id: "s1",
      reasoning: "Director chose to honour canon — regenerating shot s1.",
    });
    render(<ConflictPanel sessionId="sess1" />);

    expect(await screen.findByText(/draws a sword/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /honour canon/i }));

    await waitFor(() =>
      expect(resolveConflict).toHaveBeenCalledWith("sess1", { conflict_id: "cf_1", option: "honor_canon" }),
    );
  });

  it("collapses an already-resolved conflict to its decision record", async () => {
    getConflicts.mockResolvedValue([conflict({ resolved: true, chosen_option: "evolve_canon", reasoning: "Director chose to evolve canon." })]);
    render(<ConflictPanel sessionId="sess1" />);
    expect(await screen.findByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText(/evolve canon/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /honour canon/i })).not.toBeInTheDocument();
  });
});
