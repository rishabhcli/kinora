import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SceneTimeline from "./SceneTimeline";
import type { DirectorShot } from "../../lib/api/director";

function shot(over: Partial<DirectorShot> = {}): DirectorShot {
  return {
    shot_id: "shot1",
    beat_id: "b1",
    scene_id: "sc1",
    source_span: { word_range: [0, 50] },
    status: "accepted",
    render_mode: "reference_to_video",
    duration_s: 5,
    qa: null,
    clip_url: "http://minio:9000/kinora/c.mp4",
    reference_image_ids: [],
    ...over,
  };
}

describe("SceneTimeline", () => {
  it("shows an empty state with no shots", () => {
    render(<SceneTimeline shots={[]} selectedShotId={null} onSelect={() => {}} reRendering={new Set()} />);
    expect(screen.getByText(/no shots yet/i)).toBeInTheDocument();
  });

  it("renders per-scene lanes and a summary, calls onSelect", () => {
    const shots = [
      shot({ shot_id: "a", scene_id: "sc1", source_span: { word_range: [0, 50] } }),
      shot({ shot_id: "b", scene_id: "sc2", source_span: { word_range: [200, 250] } }),
    ];
    const onSelect = vi.fn();
    render(<SceneTimeline shots={shots} selectedShotId="a" onSelect={onSelect} reRendering={new Set()} />);

    expect(screen.getByText(/2 shots · 2 scenes/i)).toBeInTheDocument();
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(2);
    fireEvent.click(options[1]);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ shot_id: "b" }));
  });

  it("renders a note badge from noteCounts", () => {
    render(
      <SceneTimeline
        shots={[shot({ shot_id: "a" })]}
        selectedShotId={null}
        onSelect={() => {}}
        reRendering={new Set()}
        noteCounts={{ a: 3 }}
      />,
    );
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
