import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import RecommendationRail from "./RecommendationRail";
import RowSkeleton, { DiscoveryHomeSkeleton } from "./RowSkeleton";
import type { DiscoveryBook } from "../../lib/discovery/types";

function book(over: Partial<DiscoveryBook> = {}): DiscoveryBook {
  return {
    id: over.id ?? "id",
    title: over.title ?? "Title",
    author: over.author ?? "Author",
    progress: over.progress ?? 0,
    coverColor: "#000",
    coverGradient: "g",
    coverImage: "",
    textColor: "#fff",
    spineColor: "#000",
    genre: over.genre,
  };
}

const lib = [book({ id: "a", title: "A" }), book({ id: "b", title: "B" }), book({ id: "c", title: "C" })];

describe("RecommendationRail", () => {
  it("renders a titled, scrollable group of cards", () => {
    render(<RecommendationRail title="Top Picks" books={lib} />);
    expect(screen.getByRole("region", { name: "Top Picks" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Top Picks books, scrollable/ })).toBeInTheDocument();
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("C")).toBeInTheDocument();
  });

  it("shows the reason subtitle when provided", () => {
    render(<RecommendationRail title="More SF" books={lib} reason="Because you read Science Fiction" />);
    expect(screen.getByText(/Because you read Science Fiction/)).toBeInTheDocument();
  });

  it("renders nothing for an empty book list", () => {
    const { container } = render(<RecommendationRail title="Empty" books={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the scrollable group region (arrows are overflow-gated)", () => {
    // jsdom has no layout, so the overflow-driven arrows don't appear; the
    // scrollable group itself must always be present + labelled.
    render(<RecommendationRail title="Row" books={lib} />);
    expect(screen.getByRole("group", { name: /Row books, scrollable/ })).toBeInTheDocument();
  });
});

describe("RowSkeleton", () => {
  it("renders a busy placeholder row", () => {
    render(<RowSkeleton count={4} data-testid="sk" />);
    const sk = screen.getByTestId("sk");
    expect(sk).toHaveAttribute("aria-busy", "true");
  });

  it("renders a full-page skeleton", () => {
    render(<DiscoveryHomeSkeleton rows={3} />);
    expect(screen.getByTestId("discovery-home-skeleton")).toBeInTheDocument();
  });
});
