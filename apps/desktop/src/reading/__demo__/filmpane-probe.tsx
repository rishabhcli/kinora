// Dev-only probe: mount FilmPane alone and expose its imperative handle so the
// Electron verifier can drive setPlayhead directly and observe layer behaviour
// (crossfade on a play-mode src change vs. instant cut under reduced motion).
//   ?reduce=1   force reduced motion
import { useRef } from "react";
import ReactDOM from "react-dom/client";
import { FilmPane, type FilmPaneHandle } from "../FilmPane";

const reduce = new URLSearchParams(location.search).get("reduce") === "1";

function Probe() {
  const ref = useRef<FilmPaneHandle>(null);
  const w = window as unknown as {
    __pane: {
      setPlayhead(src: string, time: number, scrub: boolean): void;
      videoCount(): number;
      activeSrc(): string;
      currentTime(): number;
      paused(): boolean | null;
      duration(): number;
    };
  };
  const lastVideo = () => {
    const vids = document.querySelectorAll("video");
    return vids[vids.length - 1] as HTMLVideoElement | undefined;
  };
  w.__pane = {
    setPlayhead: (src, time, scrub) => ref.current?.setPlayhead(src, time, scrub),
    videoCount: () => document.querySelectorAll("video").length,
    activeSrc: () => lastVideo()?.currentSrc || lastVideo()?.src || "",
    currentTime: () => lastVideo()?.currentTime ?? -1,
    paused: () => lastVideo()?.paused ?? null,
    duration: () => {
      const d = lastVideo()?.duration;
      return d && Number.isFinite(d) ? d : -1;
    },
  };
  return (
    <div id="pane">
      <FilmPane ref={ref} reducedMotion={reduce} className="absolute inset-0" />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<Probe />);
