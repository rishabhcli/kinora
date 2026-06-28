// Skeleton/loading states for the discovery surface. Mirrors the shelf layout
// (a title bar + a row of cover-shaped placeholders) so the page doesn't reflow
// when real data arrives. Reuses the app's <SkeletonShimmer> primitive.
import { SkeletonShimmer } from "../SkeletonShimmer";

interface RowSkeletonProps {
  /** Number of placeholder cards. */
  count?: number;
  /** Whether to show the title placeholder. */
  withTitle?: boolean;
  "data-testid"?: string;
}

/** A single cover-shaped placeholder matching BookCard's 150px width + 3:2 cover. */
export function CardSkeleton() {
  return (
    <div className="flex-shrink-0 w-[150px]" aria-hidden>
      <SkeletonShimmer
        className="rounded-md"
        style={{ width: 150, height: 225, marginBottom: 6 }}
      />
      <SkeletonShimmer className="rounded" style={{ width: "80%", height: 11, marginBottom: 4 }} />
      <SkeletonShimmer className="rounded" style={{ width: "55%", height: 9 }} />
    </div>
  );
}

export default function RowSkeleton({
  count = 6,
  withTitle = true,
  "data-testid": testId,
}: RowSkeletonProps) {
  return (
    <section className="mb-8" aria-busy="true" aria-label="Loading books" data-testid={testId}>
      {withTitle && (
        <div className="flex items-center gap-2 mb-3 px-1">
          <SkeletonShimmer className="rounded-full" style={{ width: 4, height: 16 }} />
          <SkeletonShimmer className="rounded" style={{ width: 160, height: 16 }} />
        </div>
      )}
      <div className="flex gap-4 px-1 pb-3 overflow-hidden">
        {Array.from({ length: count }).map((_, i) => (
          <CardSkeleton key={i} />
        ))}
      </div>
    </section>
  );
}

/** A whole-page discovery skeleton: a few rows of varying length. */
export function DiscoveryHomeSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div data-testid="discovery-home-skeleton">
      {Array.from({ length: rows }).map((_, i) => (
        <RowSkeleton key={i} count={i === 0 ? 4 : 6} />
      ))}
    </div>
  );
}
