// The adaptive multi-quality ladder, pure half. Kinora's backend can render a
// shot at more than one fidelity (CLAUDE.md: turbo defaults vs the plus/preview
// quality overrides; §12.4's degradation ladder steps full-video → keyframe pan →
// illustration → audio-only). On the *client* we mirror that as a set of quality
// LEVELS the reader's pane can request, and an ABR-style controller that picks the
// highest level we can sustain given bandwidth, the committed buffer ahead, decode
// health, and the device. The controller never blanks the pane — the lowest rung
// is always playable — and it uses hysteresis so it doesn't oscillate between
// neighbouring tiers on a noisy link.
//
// This is decoupled from `lib/api.ts`: the engine maps a `QualityLevel.id` onto a
// concrete clip-variant URL (when the backend exposes variants) or simply records
// the desired tier for the scheduler. DOM-free + unit-tested.

/** A rung of the client quality ladder, richest first. `minKbps` is the sustained
 *  throughput we want before *upgrading* to this rung; `tier` maps onto §12.4. */
export interface QualityLevel {
  /** stable id used by the engine to resolve a variant / annotate telemetry */
  id: string;
  /** human label for the observability panel */
  label: string;
  /** §12.4 degradation rung this level corresponds to */
  tier: "video-hd" | "video-sd" | "keyframe-pan" | "illustration" | "audio-text";
  /** approximate pixel height of the variant (for device-cap gating) */
  height: number;
  /** sustained kbps wanted before selecting this rung */
  minKbps: number;
  /** does this rung require GPU/WebGL compositing to look right? */
  needsGpu: boolean;
}

/** The default client ladder, richest → leanest. Heights/bitrates are sized for
 *  the 9:16 vertical film pane (≈720×1280 at the top). The bottom rung is the
 *  §12.4/ladder-bottom-rung "audio + karaoke text" card and is ALWAYS selectable
 *  (minKbps 0) so the pane never has nothing to show. */
export const DEFAULT_LADDER: readonly QualityLevel[] = [
  { id: "hd", label: "HD film", tier: "video-hd", height: 1280, minKbps: 4500, needsGpu: false },
  { id: "sd", label: "Film", tier: "video-sd", height: 720, minKbps: 1800, needsGpu: false },
  { id: "pan", label: "Keyframe pan", tier: "keyframe-pan", height: 720, minKbps: 600, needsGpu: false },
  { id: "still", label: "Illustration", tier: "illustration", height: 720, minKbps: 150, needsGpu: false },
  { id: "audio", label: "Read-along", tier: "audio-text", height: 0, minKbps: 0, needsGpu: false },
] as const;

/** Inputs the controller fuses each decision tick. All optional except the
 *  ladder is implied; missing signals are treated as "no constraint". */
export interface QualityInputs {
  /** sustained estimate in kbps (BandwidthEstimator.kbps()) */
  kbps?: number;
  /** committed seconds of film buffered ahead of the reader (SSE buffer_state) */
  bufferAheadS?: number | null;
  /** the decoder's recent health (perf/decodeStats) */
  decodeHealth?: "good" | "degraded" | "stalled";
  /** rolling rAF jank ratio [0,1] (perf/frameStats) — high jank caps GPU rungs */
  jankRatio?: number;
  /** device ceiling: max usable variant height (e.g. devicePixelRatio × pane px) */
  maxHeight?: number;
  /** is a GPU compositor available? gates `needsGpu` rungs (none today, future-proof) */
  gpuAvailable?: boolean;
  /** honour the data-saver / reduced-motion intent by capping at SD or lower */
  saveData?: boolean;
}

export interface QualityConfig {
  ladder?: readonly QualityLevel[];
  /** require kbps ≥ minKbps × this to UPGRADE (headroom so we don't flap up) */
  upgradeHeadroom?: number;
  /** drop down a rung once kbps falls below its minKbps × this */
  downgradeSlack?: number;
  /** seconds of committed buffer below which we refuse to upgrade (risk of starve) */
  safeBufferS?: number;
  /** minimum ms between an UPGRADE and the next change (anti-flap dwell) */
  upgradeDwellMs?: number;
}

const DEFAULT_UP_HEADROOM = 1.3;
const DEFAULT_DOWN_SLACK = 0.85;
const DEFAULT_SAFE_BUFFER_S = 4;
const DEFAULT_UPGRADE_DWELL_MS = 8000;

export interface QualityDecision {
  level: QualityLevel;
  /** index of `level` within the ladder (0 = richest) */
  index: number;
  /** why we landed here (telemetry / the observability panel) */
  reason: string;
  /** did this decision change the level vs. the previous one? */
  changed: boolean;
}

/** The pure controller: given the current signals and the *previous* selected
 *  index, pick the rung to use now. Encodes the safety rules:
 *   - never select a `needsGpu` rung without a GPU,
 *   - never select a rung taller than the device ceiling,
 *   - require bandwidth headroom + a safe buffer to UPGRADE,
 *   - downgrade promptly when bandwidth/decoder can't sustain the current rung,
 *   - honour data-saver,
 *   - the bottom rung is always valid (no black pane).
 *  Hysteresis (dwell) lives in {@link QualityController}; this fn is stateless. */
export function selectQuality(
  inputs: QualityInputs,
  prevIndex: number,
  config: QualityConfig = {},
): QualityDecision {
  const ladder = config.ladder ?? DEFAULT_LADDER;
  const upHeadroom = config.upgradeHeadroom ?? DEFAULT_UP_HEADROOM;
  const downSlack = config.downgradeSlack ?? DEFAULT_DOWN_SLACK;
  const safeBuffer = config.safeBufferS ?? DEFAULT_SAFE_BUFFER_S;

  const kbps = inputs.kbps ?? Infinity;
  const gpuOk = inputs.gpuAvailable ?? true; // no GPU-only rungs ship today
  const maxHeight = inputs.maxHeight ?? Infinity;
  const buffer = inputs.bufferAheadS ?? Infinity;

  // The richest rung the device + (optional) data-saver intent even permits.
  const ceilingIndex = ((): number => {
    let i = 0;
    for (; i < ladder.length; i++) {
      const lvl = ladder[i];
      const heightOk = lvl.height <= maxHeight;
      const gpuRungOk = !lvl.needsGpu || gpuOk;
      const saveOk = !inputs.saveData || lvl.tier !== "video-hd";
      if (heightOk && gpuRungOk && saveOk) break;
    }
    return Math.min(i, ladder.length - 1);
  })();

  // A hard floor forced by decoder/jank distress, regardless of bandwidth.
  const distressFloor = ((): number | null => {
    if (inputs.decodeHealth === "stalled") return indexOfTier(ladder, "keyframe-pan");
    if (inputs.decodeHealth === "degraded") return indexOfTier(ladder, "video-sd");
    if ((inputs.jankRatio ?? 0) >= 0.25) return indexOfTier(ladder, "video-sd");
    return null;
  })();

  // Bandwidth-feasible richest rung: highest rung whose minKbps fits the link.
  // Use the downgrade slack so a rung we're ALREADY on stays feasible a bit longer.
  let bandwidthIndex = ladder.length - 1;
  for (let i = 0; i < ladder.length; i++) {
    const onThis = i === prevIndex;
    const need = ladder[i].minKbps * (onThis ? downSlack : upHeadroom);
    if (kbps >= need) {
      bandwidthIndex = i;
      break;
    }
  }

  // Start from the most-constrained of (ceiling, bandwidth) — bigger index = leaner.
  let target = Math.max(ceilingIndex, bandwidthIndex);
  if (distressFloor != null) target = Math.max(target, distressFloor);

  // Anti-yo-yo on UPGRADES only: refuse to climb if the buffer is unsafe. (We
  // always allow DOWNGRADES — leaner is safer.) An upgrade = a smaller index.
  let reason = "feasible";
  if (target < prevIndex && buffer < safeBuffer) {
    target = prevIndex; // hold; the dwell logic in the controller also gates this
    reason = "hold-unsafe-buffer";
  } else if (target < prevIndex) {
    reason = "upgrade";
  } else if (target > prevIndex) {
    reason =
      distressFloor != null && target === distressFloor
        ? `downgrade-${inputs.decodeHealth ?? "jank"}`
        : "downgrade-bandwidth";
  } else {
    reason = "steady";
  }

  target = clampIndex(target, ladder.length);
  return {
    level: ladder[target],
    index: target,
    reason,
    changed: target !== prevIndex,
  };
}

/** Stateful wrapper adding hysteresis: an upgrade must "dwell" before the next
 *  change so a flickering estimate can't ping-pong the tier. Downgrades are
 *  immediate (safety). */
export class QualityController {
  private index: number;
  private lastUpgradeAt = -Infinity;
  private readonly config: QualityConfig;
  private readonly ladder: readonly QualityLevel[];

  constructor(config: QualityConfig = {}, startIndex = 1 /* SD: a safe default */) {
    this.config = config;
    this.ladder = config.ladder ?? DEFAULT_LADDER;
    this.index = clampIndex(startIndex, this.ladder.length);
  }

  /** Decide the rung for `nowMs`. Honours the upgrade dwell; lets downgrades
   *  through immediately. */
  update(inputs: QualityInputs, nowMs: number): QualityDecision {
    const raw = selectQuality(inputs, this.index, this.config);
    const dwell = this.config.upgradeDwellMs ?? DEFAULT_UPGRADE_DWELL_MS;
    if (raw.index < this.index) {
      // Upgrade requested — only honour it once the dwell has elapsed.
      if (nowMs - this.lastUpgradeAt < dwell) {
        return { level: this.ladder[this.index], index: this.index, reason: "upgrade-dwell", changed: false };
      }
      this.lastUpgradeAt = nowMs;
    }
    this.index = raw.index;
    return raw;
  }

  current(): QualityLevel {
    return this.ladder[this.index];
  }

  currentIndex(): number {
    return this.index;
  }

  reset(startIndex = 1): void {
    this.index = clampIndex(startIndex, this.ladder.length);
    this.lastUpgradeAt = -Infinity;
  }
}

function indexOfTier(ladder: readonly QualityLevel[], tier: QualityLevel["tier"]): number {
  const i = ladder.findIndex((l) => l.tier === tier);
  return i === -1 ? ladder.length - 1 : i;
}

function clampIndex(i: number, len: number): number {
  if (i < 0) return 0;
  if (i > len - 1) return len - 1;
  return i;
}
