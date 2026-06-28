// Public surface of the discovery package — the home shell imports from here.
export { default as DiscoveryHome } from "./DiscoveryHome";
export { default as DiscoverySearch } from "./DiscoverySearch";
export { default as CommandPalette, announcePaletteOpen } from "./CommandPalette";
export { default as RecommendationRail } from "./RecommendationRail";
export { default as ContinueReadingRow } from "./ContinueReadingRow";
export { default as BookPreviewCard } from "./BookPreviewCard";
export { default as RowSkeleton, DiscoveryHomeSkeleton } from "./RowSkeleton";
export { useDiscovery, type DiscoveryApi } from "./useDiscovery";
export { useCommandPalette } from "./useCommandPalette";
export { useRovingGrid, type RovingGrid } from "./useRovingGrid";
export { buildCommands, type CommandContext } from "./commands";
export { ensureDiscoveryStyles } from "./styleInjection";
