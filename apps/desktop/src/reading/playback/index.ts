// Public surface of the next-generation playback subsystems for the reading room.
// One import point for the engine + the shell (Phase 7 integration) so adopting a
// subsystem is a single named import, not a path archaeology dig. Everything here
// is additive over today's FilmPane/useScrollFilm and preserves the no-black-frame
// guarantee (see DESIGN.md). The pure `timeline.ts` contract is untouched.

// --- GPU compositor (optional enhancement over the CSS crossfade) ---
export { WebGLCompositor } from "../gl/webglCompositor";
export type { FrameSource, RenderOptions, CompositorStatus, LayerSlot } from "../gl/webglCompositor";
export { GpuFilmOverlay } from "../gl/GpuFilmOverlay";
export type { GpuFilmOverlayProps } from "../gl/GpuFilmOverlay";
export { decideCompositor, probeGl, NO_GL } from "../gl/capabilities";
export type { GlCapabilities, CompositorDecision } from "../gl/capabilities";
export {
  NEUTRAL_GRADE,
  GRADE_PRESETS,
  gradeByName,
  applyGrade,
  lerpGrade,
} from "../gl/grade";
export type { FilmGrade, RGB } from "../gl/grade";
export { transitionAt, sampleTransition, easeInOut, smoothstep, midBell } from "../gl/transitions";
export type { TransitionKind, TransitionFrame } from "../gl/transitions";

// --- performance instrumentation (§12.5) ---
export { FrameStats, percentile } from "../perf/frameStats";
export type { FrameStatsSnapshot, FrameSample, FrameStatsConfig } from "../perf/frameStats";
export { DecodeStats, classifyDecode } from "../perf/decodeStats";
export type { DecodeHealth, DecodeDelta, PlaybackQualityReading } from "../perf/decodeStats";
export { usePerfMonitor } from "../perf/usePerfMonitor";
export type { PerfMonitor, PerfSnapshot, UsePerfMonitorOptions } from "../perf/usePerfMonitor";
export { buildObservability, gradeHealth } from "../perf/observability";
export type { ObservabilitySnapshot, ObservabilityInput, HealthGrade } from "../perf/observability";

// --- adaptive multi-quality streaming (§12.4, §4.6) ---
export { BandwidthEstimator } from "../streaming/bandwidth";
export type { ThroughputSample, BandwidthConfig } from "../streaming/bandwidth";
export {
  DEFAULT_LADDER,
  QualityController,
  selectQuality,
} from "../streaming/qualityLadder";
export type { QualityLevel, QualityDecision, QualityInputs, QualityConfig } from "../streaming/qualityLadder";
export { useAdaptiveQuality } from "../streaming/useAdaptiveQuality";
export type { AdaptiveQuality, AdaptiveSignals, UseAdaptiveQualityOptions } from "../streaming/useAdaptiveQuality";
export { makeInstrumentedFetch, instrumentForEstimator } from "../streaming/instrumentedFetch";
export { simulateAbr, levelById } from "../streaming/abrSim";
export type { TraceSample, SimResult, SimStep } from "../streaming/abrSim";

// --- frame-accurate scrubbing ---
export { frameCount, quantizeTime, quantizePosition, stepFrames, sameFrame } from "../scrub/frameClock";
export type { FrameClockConfig, FrameInfo } from "../scrub/frameClock";
export { watchPresentedFrames, hasFrameCallback } from "../scrub/requestVideoFrameCallback";
export type { PresentedFrame, FrameCallbackVideo } from "../scrub/requestVideoFrameCallback";
export { planSeek } from "../scrub/seekPlan";
export type { SeekPlan, SeekPlanInput, SeekMode } from "../scrub/seekPlan";

// --- offline service-worker cache ---
export {
  classifyAsset,
  strategyFor,
  clipCacheName,
  pageCacheName,
  isPageToSw,
  isSwToPage,
  SW_CACHE_VERSION,
} from "../offline/swProtocol";
export type { AssetKind, CacheStrategy, PageToSw, SwToPage } from "../offline/swProtocol";
export { buildManifest, planEviction, priorityScore } from "../offline/manifest";
export type { PrecacheManifest, ManifestInput, ManifestSegment, EvictionPlan } from "../offline/manifest";
export { useOfflineCache } from "../offline/useOfflineCache";
export type { OfflineCache, OfflineStatus } from "../offline/useOfflineCache";

// --- gesture / touch / picture-in-picture ---
export { ScrubGesture } from "../gesture/scrubGesture";
export type { ScrubGestureConfig, ScrubDelta, FlingResult, PointerSample } from "../gesture/scrubGesture";
export { canUsePip, pipState, enterPip, exitPip, togglePip } from "../gesture/pictureInPicture";
export type { PipState, PipVideo, PipDocument } from "../gesture/pictureInPicture";

// --- described video (accessibility) ---
export {
  buildTrack,
  activeCue,
  spokenDurationS,
  decideAnnounce,
  initialAnnouncerState,
} from "../describedVideo/describedVideo";
export type { DescriptionCue, DescriptionTrack, AnnouncerState } from "../describedVideo/describedVideo";
