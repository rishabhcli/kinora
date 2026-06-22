import { hashSeed } from "./kenburns";

// Deterministic, on-brand cover art for books without a thumbnail — so the
// Apple-Books-style shelf stays handsome even before a real cover exists.
export function coverGradient(seed: string): string {
  const h = hashSeed(seed);
  const hue1 = h % 360;
  const hue2 = (hue1 + 40 + (h % 40)) % 360;
  return `linear-gradient(150deg, hsl(${hue1} 58% 32%), hsl(${hue2} 52% 16%))`;
}

export function initials(title: string): string {
  const words = title.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}
