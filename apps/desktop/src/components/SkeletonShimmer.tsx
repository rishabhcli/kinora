import { useState } from "react";

interface SkeletonShimmerProps {
  className?: string;
  style?: React.CSSProperties;
  children?: React.ReactNode;
}

export function SkeletonShimmer({ className = "", style }: SkeletonShimmerProps) {
  return (
    <div
      className={`${className} skeleton-shimmer`}
      style={{
        ...style,
        background: "rgba(255,255,255,0.04)",
        overflow: "hidden",
      }}
    >
      <div className="skeleton-shimmer-overlay" />
    </div>
  );
}

interface BookCoverImageProps {
  src: string;
  alt: string;
  className?: string;
  style?: React.CSSProperties;
  fallbackBackground?: string;
  onLoad?: (e: React.SyntheticEvent<HTMLImageElement>) => void;
  onError?: (e: React.SyntheticEvent<HTMLImageElement>) => void;
}

export function BookCoverImage({
  src,
  alt,
  className = "",
  style,
  fallbackBackground,
  onLoad,
  onError,
}: BookCoverImageProps) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  const showShimmer = !loaded && !error;
  const showFallback = error && fallbackBackground;

  return (
    <>
      {showShimmer && (
        <SkeletonShimmer
          className={`absolute inset-0 ${className}`}
          style={style}
        />
      )}
      {showFallback && (
        <div
          className={`absolute inset-0 ${className}`}
          style={{ ...style, background: fallbackBackground }}
        />
      )}
      {!error && (
        <img
          src={src}
          alt={alt}
          className={className}
          style={{
            ...style,
            opacity: loaded ? 1 : 0,
            transition: "opacity 0.4s ease",
          }}
          loading="lazy"
          onLoad={(e) => {
            const img = e.target as HTMLImageElement;
            if (img.naturalWidth <= 1) {
              setError(true);
              return;
            }
            setLoaded(true);
            onLoad?.(e);
          }}
          onError={(e) => {
            setError(true);
            onError?.(e);
          }}
        />
      )}
    </>
  );
}
