import { useRef, useState } from "react";

import { sessions } from "../../api/client";
import type { CanonGraph, SessionMode, Shot } from "../../api/types";
import { useEventsStore } from "../../stores/eventsStore";
import { useSessionStore } from "../../stores/sessionStore";
import type { SyncEngine } from "../../sync/SyncEngine";
import { useSyncSnapshot } from "../../sync/useSyncEngine";
import { WarningIcon } from "../common/icons";
import { BufferIndicator } from "./BufferIndicator";
import { ModeSwitch } from "./ModeSwitch";
import { VideoPlayer } from "./VideoPlayer";
import { AgentActivityFeed } from "./director/AgentActivityFeed";
import { CanonEditor } from "./director/CanonEditor";
import { CommentComposer } from "./director/CommentComposer";
import { RegionSelect, type CapturedRegion } from "./director/RegionSelect";
import { ShotTimeline } from "./director/ShotTimeline";

interface VideoStageProps {
  engine: SyncEngine;
  sessionId: string;
  bookId: string;
  shots: Shot[];
  canon: CanonGraph | null;
  onCanonEdited: (affectedShotIds: string[]) => void;
}

type DirectorTab = "timeline" | "canon" | "feed";

function toBase64(dataUrl: string): string {
  const i = dataUrl.indexOf(",");
  return i >= 0 ? dataUrl.slice(i + 1) : dataUrl;
}

export function VideoStage({
  engine,
  sessionId,
  bookId,
  shots,
  canon,
  onCanonEdited,
}: VideoStageProps) {
  const snap = useSyncSnapshot(engine);
  const setSessionMode = useSessionStore((s) => s.setMode);
  const budgetRemaining = useEventsStore((s) => s.budgetRemaining);

  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [tab, setTab] = useState<DirectorTab>("timeline");
  const [region, setRegion] = useState<CapturedRegion | null>(null);
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);

  const isDirector = snap.mode === "director";
  const targetShotId = selectedShotId ?? snap.currentShotId;

  const changeMode = (mode: SessionMode) => {
    engine.setMode(mode);
    setSessionMode(mode);
    if (mode === "director") setPlaying(false);
  };

  const onTogglePlay = () => setPlaying((p) => !p);

  const submitComment = async (note: string) => {
    if (!targetShotId) return;
    await sessions.comment(sessionId, {
      shot_id: targetShotId,
      region_png: region ? toBase64(region.dataUrl) : "",
      note,
    });
  };

  return (
    <div className="flex h-full flex-col bg-kinora-panel/40">
      <div className="flex items-center justify-between gap-3 border-b border-kinora-line/60 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-kinora-mist">
            {isDirector ? "Director" : "Viewer"}
          </p>
          <p className="truncate text-xs text-kinora-muted">
            {snap.currentShotId ? `Shot ${snap.currentShotId}` : "Awaiting first shot"}
          </p>
        </div>
        <ModeSwitch mode={snap.mode} onChange={changeMode} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
        <div className="p-4">
          <div className="relative">
            <VideoPlayer
              videoRef={videoRef}
              src={snap.videoSrc}
              preloadSrc={snap.preloadSrc}
              bridging={snap.bridging}
              bridgeKeyframeUrl={snap.bridgeKeyframeUrl}
              seekNonce={snap.seekNonce}
              seekToS={snap.seekToS}
              playing={playing}
              onTogglePlay={onTogglePlay}
              onTime={(t) => engine.onVideoTime(t)}
              onEnded={() => engine.onVideoEnded()}
              bridgeSeed={snap.currentShotId}
            />
            {isDirector ? (
              <RegionSelect
                videoRef={videoRef}
                active={isDirector}
                onRegion={(r) => {
                  setRegion(r);
                  setCaptureError(null);
                }}
                onError={setCaptureError}
              />
            ) : null}
          </div>

          {!isDirector ? (
            <div className="mt-3">
              <BufferIndicator committedSecondsAhead={snap.committedSecondsAhead} />
              {budgetRemaining !== null ? (
                <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-kinora-warn">
                  <WarningIcon className="h-3.5 w-3.5" />
                  Budget is low ({Math.round(budgetRemaining)}s) — riding the keyframe ladder.
                </p>
              ) : null}
            </div>
          ) : (
            <div className="mt-3 space-y-4">
              <CommentComposer
                shotId={targetShotId}
                regionDataUrl={region?.dataUrl ?? null}
                onClear={() => setRegion(null)}
                onSubmit={submitComment}
              />
              {captureError ? (
                <p className="text-xs text-kinora-danger">{captureError}</p>
              ) : null}

              <div className="glass-segment inline-flex gap-1 rounded-full p-1">
                {(["timeline", "canon", "feed"] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTab(t)}
                    className={`rounded-full px-3.5 py-1.5 text-xs font-medium capitalize transition-colors ${
                      tab === t ? "bg-kinora-glow text-white" : "text-kinora-muted hover:text-kinora-mist"
                    }`}
                  >
                    {t === "feed" ? "Agent feed" : t}
                  </button>
                ))}
              </div>

              <div className="glass rounded-2xl p-4">
                {tab === "timeline" ? (
                  <ShotTimeline
                    shots={shots}
                    currentShotId={snap.currentShotId}
                    onSelect={(shot) => {
                      setSelectedShotId(shot.shot_id);
                      engine.seek(shot.source_span.word_range[0]);
                    }}
                  />
                ) : null}
                {tab === "canon" ? (
                  <CanonEditor bookId={bookId} canon={canon} onEdited={onCanonEdited} />
                ) : null}
                {tab === "feed" ? <AgentActivityFeed sessionId={sessionId} /> : null}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
