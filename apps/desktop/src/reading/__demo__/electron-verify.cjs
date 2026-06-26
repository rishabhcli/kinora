// Runtime verification for the Scroll Film Engine, driven through Electron's
// Chromium (which — unlike headless Playwright chromium — decodes the bundled
// H.264 films). Needs the Vite dev server on :5199.
//   node_modules/.bin/electron apps/desktop/src/reading/__demo__/electron-verify.cjs
//
// One shown window, reused across pages: hidden windows don't paint (CSS
// transitions / transitionend for the crossfade promote, and 60fps rAF, would
// stall), and destroying a window mid-video-playback crashes the GPU process.
const { app, BrowserWindow } = require("electron");

const BASE = "http://localhost:5199/src/reading/__demo__";
const results = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function record(name, pass, detail) {
  results.push({ name, pass });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

let WIN = null;
const MSGS = [];
const js = (expr) => WIN.webContents.executeJavaScript(expr, true);

async function nav(url) {
  MSGS.length = 0;
  for (let attempt = 0; ; attempt++) {
    try {
      await WIN.loadURL(url);
      break;
    } catch (e) {
      if (attempt >= 3) throw e;
      await sleep(500); // transient ERR_FAILED right after a heavy page swap
    }
  }
  await js(
    `(()=>{window.__errs=[];window.addEventListener('error',e=>window.__errs.push(String(e.message)));window.addEventListener('unhandledrejection',e=>window.__errs.push('rejection:'+e.reason));})()`,
  ).catch(() => {});
}

async function waitForVideo() {
  for (let i = 0; i < 60; i++) {
    const d = await js(`window.__kinora ? window.__kinora.read().activeDuration : -1`);
    if (d > 0) return d;
    await sleep(150);
  }
  return -1;
}

async function setScroll(f) {
  await js(
    `(()=>{const s=document.querySelector('[data-testid="reading-scroll"]');s.scrollTop=${f}*(s.scrollHeight-s.clientHeight);s.dispatchEvent(new Event('scroll'));})()`,
  );
}

async function testFallbackScrub() {
  await nav(`${BASE}/scrub-demo.html?mode=fallback`);
  const dur = await waitForVideo();
  record("fallback: bundled film decodes (H.264)", dur > 0, `duration=${dur}`);
  const range = await js(`window.__kinora.read().scrollRange`);
  record("fallback: text column is scrollable", range > 0, `range=${range}px`);

  for (const f of [0.5, 0.9, 0.25, 0.75]) {
    await setScroll(f);
    // A flick scrubs (pauses + pins currentTime) for ~200ms before settling back to
    // play. Poll for that paused scrub frame rather than racing a fixed instant.
    const expected = f * dur;
    const tol = Math.max(0.4, 0.12 * dur);
    let st = {};
    for (let i = 0; i < 12; i++) {
      await sleep(25);
      st = await js(`window.__kinora.read()`);
      if (st.activePaused === true && Math.abs(st.activeTime - expected) <= tol) break;
    }
    record(
      `fallback: scrub @${f} pins currentTime≈${expected.toFixed(2)}s`,
      st.activePaused === true && Math.abs(st.activeTime - expected) <= tol,
      `currentTime=${Number(st.activeTime).toFixed(2)} paused=${st.activePaused}`,
    );
  }
  const errs = await js(`window.__errs || []`);
  const bad = MSGS.filter((m) => /uncaught|is not a function|cannot read|warning:/i.test(m));
  record("fallback: no runtime errors", errs.length === 0 && bad.length === 0, [...errs, ...bad].slice(0, 2).join(" | "));

  // Frame cadence under a continuous flick: drive scroll in the page's own rAF and
  // measure real frame deltas while the engine scrubs + decodes on the same thread.
  const fps = await js(`new Promise((res)=>{
    const s=document.querySelector('[data-testid="reading-scroll"]');
    const max=s.scrollHeight-s.clientHeight; let t0=performance.now(), last=t0, deltas=[], pos=0, dir=1;
    function loop(now){ deltas.push(now-last); last=now;
      pos+=dir*max*0.05; if(pos>=max){pos=max;dir=-1} if(pos<=0){pos=0;dir=1}
      s.scrollTop=pos; s.dispatchEvent(new Event('scroll'));
      if(now-t0<900) requestAnimationFrame(loop);
      else { const d=deltas.slice(1).sort((a,b)=>a-b); res({count:deltas.length, median:d[Math.floor(d.length/2)], p95:d[Math.floor(d.length*0.95)]}); }
    } requestAnimationFrame(loop);
  })`);
  record(
    "60fps: main thread sustains ~60fps under continuous scroll",
    fps.median <= 20 && fps.count >= 45,
    `frames=${fps.count}/~0.9s median=${Number(fps.median).toFixed(1)}ms p95=${Number(fps.p95).toFixed(1)}ms`,
  );
}

async function testLiveHandoff() {
  await nav(`${BASE}/scrub-demo.html?mode=live`);
  await waitForVideo();
  for (const [f, want] of [
    [0.1, "film-01"],
    [0.6, "film-03"],
    [0.95, "film-04"],
  ]) {
    await setScroll(f);
    await sleep(450); // allow the segment's clip to swap in + decode
    const src = await js(`window.__kinora.read().activeSrc`);
    record(`live: scroll @${f} hands off to ${want}`, String(src).includes(want), `activeSrc=${String(src).split("/").pop()}`);
  }
}

async function probeLayers(reduce) {
  await nav(`${BASE}/filmpane-probe.html?reduce=${reduce}`);
  await js(`window.__pane.setPlayhead('/generated/film-01.mp4',0,false)`);
  await sleep(500);
  await js(`window.__pane.setPlayhead('/generated/film-02.mp4',0,false)`); // play-mode src change
  let sawTwo = false;
  for (let i = 0; i < 18; i++) {
    if ((await js(`window.__pane.videoCount()`)) >= 2) sawTwo = true;
    await sleep(100);
  }
  return { sawTwo, final: await js(`window.__pane.videoCount()`) };
}

async function testCrossfadeVsReduced() {
  const a = await probeLayers(0);
  record("crossfade (normal): src change shows 2 layers, then promotes to 1", a.sawTwo && a.final === 1, `sawTwo=${a.sawTwo} final=${a.final}`);
  const b = await probeLayers(1);
  record("reduced motion: src change is an instant cut (never 2 layers)", !b.sawTwo && b.final === 1, `everTwo=${b.sawTwo} final=${b.final}`);
}

app.whenReady().then(async () => {
  if (process.platform === "darwin" && app.dock) app.dock.hide();
  WIN = new BrowserWindow({ show: true, x: 60, y: 60, width: 1280, height: 900, focusable: false, webPreferences: { backgroundThrottling: false } });
  WIN.webContents.on("console-message", (_e, _lvl, msg) => MSGS.push(String(msg)));
  const hardTimeout = setTimeout(() => {
    console.log("FAIL  global timeout");
    app.exit(1);
  }, 90000);
  try {
    await testFallbackScrub();
    await testLiveHandoff();
    await testCrossfadeVsReduced();
  } catch (e) {
    console.log("FAIL  driver threw — " + (e && e.stack ? e.stack : e));
  }
  clearTimeout(hardTimeout);
  const failed = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  app.exit(failed > 0 ? 1 : 0);
});
