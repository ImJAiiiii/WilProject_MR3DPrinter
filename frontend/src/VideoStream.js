// src/VideoStream.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import "./VideoStream.css";
import PrintProgress from "./PrintProgress";
import PrintControls from "./PrintControls";
import { API_BASE } from "./api";

const PRINTER_ID = process.env.REACT_APP_PRINTER_ID || "prusa-core-one";

// ===== Stream envs =====
const STREAM_URL_RAW = (process.env.REACT_APP_STREAM_URL || "").trim();
const STREAM_MODE_ENV = (process.env.REACT_APP_STREAM_MODE || "").trim().toLowerCase();
const DIRECT_SNAPSHOT_RAW = (process.env.REACT_APP_SNAPSHOT_URL || "").trim();
const SNAPSHOT_FPS_ENV = Number(process.env.REACT_APP_SNAPSHOT_FPS || "");

// ===== helpers =====
function decideStreamMode(url, envMode) {
  if (!url) return "snapshot";
  if (envMode === "mjpeg" || envMode === "video") return envMode;
  if (/action=stream|mjpg|mjpeg/i.test(url)) return "mjpeg";
  return "video";
}

function isSameSecureScheme(url) {
  try {
    const u = new URL(url, window.location.origin);
    if (window.location.protocol === "https:") return u.protocol === "https:";
    return true;
  } catch {
    return false;
  }
}

export default function VideoStream({
  estimatedSeconds = 0,
  startedAt = null,
  state = "idle",
  job,
  queueNumber,
  objectFit = "cover", // ให้เต็มเฟรมเลย
  targetFps =
    Number.isFinite(SNAPSHOT_FPS_ENV) && SNAPSHOT_FPS_ENV > 0
      ? SNAPSHOT_FPS_ENV
      : 8,
  minFpsHidden = 3,
}) {
  // ---------- mode/url ----------
  const streamUrl = STREAM_URL_RAW || "";
  const mode = useMemo(
    () => decideStreamMode(streamUrl, STREAM_MODE_ENV),
    [streamUrl]
  );
  const isStreamMJPEG = mode === "mjpeg";
  const isStreamVideo = mode === "video";
  const [hint, setHint] = useState("");

  const baseSnapshotUrl = useMemo(() => {
    if (DIRECT_SNAPSHOT_RAW && isSameSecureScheme(DIRECT_SNAPSHOT_RAW)) {
      return DIRECT_SNAPSHOT_RAW;
    }
    if (DIRECT_SNAPSHOT_RAW && !isSameSecureScheme(DIRECT_SNAPSHOT_RAW)) {
      setHint("Blocked mixed-content: fallback to backend proxy snapshot");
    }
    const base = (API_BASE || "").replace(/\/+$/, "");
    return `${base}/printers/${encodeURIComponent(PRINTER_ID)}/snapshot`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const useSnapshot =
    !streamUrl || (streamUrl && !isSameSecureScheme(streamUrl));

  // ---------- states ----------
  const [isPlaying, setIsPlaying] = useState(true);

  // ---------- <video> ----------
  const videoRef = useRef(null);
  useEffect(() => {
    if (!isStreamVideo || useSnapshot) return;
    const el = videoRef.current;
    if (!el) return;

    try {
      el.pause();
      el.removeAttribute("src");
      el.load?.();
    } catch {}

    el.src = streamUrl;
    el.muted = true;
    el.playsInline = true;
    el.preload = "none";

    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    const onError = () => {
      setIsPlaying(false);
      setHint("Stream error → snapshot fallback");
    };

    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("error", onError);

    (async () => {
      try {
        await el.play();
      } catch {
        setIsPlaying(false);
      }
    })();

    return () => {
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("error", onError);
      try {
        el.pause();
        el.removeAttribute("src");
        el.load?.();
      } catch {}
    };
  }, [isStreamVideo, streamUrl, useSnapshot]);

  // ---------- Snapshot: double-buffer DOM (no React state per frame) ----------
  const imgARef = useRef(null);
  const imgBRef = useRef(null);
  const [snapshotUnstable, setSnapshotUnstable] = useState(false);
  const errCountRef = useRef(0);
  const runningRef = useRef(false);

  useEffect(() => {
    if (!useSnapshot) return;

    runningRef.current = true;
    errCountRef.current = 0;
    setSnapshotUnstable(false);

    let visible = "A"; // ตัวไหนโชว์อยู่ตอนนี้ ("A" หรือ "B")
    let destroyed = false;
    let timerId = null;

    const loader = new Image();
    loader.crossOrigin = "anonymous";

    const getVisibleImg = () =>
      visible === "A" ? imgARef.current : imgBRef.current;
    const getHiddenImg = () =>
      visible === "A" ? imgBRef.current : imgARef.current;

    const swapBuffers = () => {
      const vis = getVisibleImg();
      const hid = getHiddenImg();
      if (!vis || !hid) return;
      // cross-fade แบบง่าย: ตัวใหม่ขึ้น 1, ตัวเก่าเป็น 0
      hid.style.opacity = "1";
      vis.style.opacity = "0";
      visible = visible === "A" ? "B" : "A";
    };

    const makeUrl = () =>
      `${baseSnapshotUrl}${
        baseSnapshotUrl.includes("?") ? "&" : "?"
      }t=${Date.now()}`;

    const schedule = (delayMs) => {
      timerId = setTimeout(() => {
        if (runningRef.current && !destroyed) {
          requestAnimationFrame(tick);
        }
      }, delayMs);
    };

    const tick = () => {
      if (!runningRef.current || destroyed || !isPlaying) return;

      const hidden =
        typeof document !== "undefined" &&
        document.visibilityState === "hidden";
      const wantFps = hidden
        ? Math.max(minFpsHidden, Math.floor(targetFps / 3))
        : targetFps;
      const budget = Math.max(0, 1000 / Math.max(1, wantFps));

      const hidImg = getHiddenImg();
      if (!hidImg) {
        schedule(budget);
        return;
      }

      loader.onload = () => {
        if (!runningRef.current || destroyed) return;
        hidImg.src = loader.src; // โหลดเสร็จแล้วค่อยอัปเดต src ของ buffer ที่ซ่อน
        swapBuffers(); // แล้วค่อยสลับโชว์ → ไม่มีเฟรมดำ
        errCountRef.current = 0;
        setSnapshotUnstable(false);
        schedule(budget);
      };

      loader.onerror = () => {
        errCountRef.current = Math.min(99, errCountRef.current + 1);
        if (errCountRef.current >= 3) setSnapshotUnstable(true);
        schedule(Math.max(300, budget));
      };

      loader.src = makeUrl();
    };

    // เริ่มด้วย tick แรก
    requestAnimationFrame(tick);

    return () => {
      destroyed = true;
      runningRef.current = false;
      if (timerId) clearTimeout(timerId);
      loader.onload = loader.onerror = null;
    };
  }, [useSnapshot, baseSnapshotUrl, isPlaying, targetFps, minFpsHidden]);

  // ---------- controls ----------
  const togglePlayPause = async () => {
    if (isStreamVideo && !useSnapshot) {
      const el = videoRef.current;
      if (!el) return;
      if (el.paused || el.ended) {
        try {
          await el.play();
          setIsPlaying(true);
        } catch {}
      } else {
        el.pause();
        setIsPlaying(false);
      }
    } else {
      setIsPlaying((v) => !v);
    }
  };

  const mixedBlocked = streamUrl && !isSameSecureScheme(streamUrl);

  return (
    <div className="video-stream">
      <div className="video-block">
        <div className="video-wrapper">
          {isStreamVideo && !useSnapshot ? (
            <video
              ref={videoRef}
              className="video-el"
              autoPlay
              muted
              playsInline
              preload="none"
            />
          ) : isStreamMJPEG && !useSnapshot ? (
            <img
              className="video-el"
              alt="live"
              src={streamUrl}
              decoding="async"
              fetchpriority="high"
              referrerPolicy="no-referrer"
            />
          ) : (
            <>
              {/* buffer A */}
              <img
                ref={imgARef}
                className="video-el"
                alt=""
                decoding="async"
                fetchpriority="high"
                referrerPolicy="no-referrer"
                style={{
                  opacity: 1, // เริ่มให้ A โชว์ก่อน
                  transition: "opacity 80ms linear",
                  objectFit,
                }}
              />
              {/* buffer B */}
              <img
                ref={imgBRef}
                className="video-el"
                alt="Printer camera snapshot"
                decoding="async"
                fetchpriority="high"
                referrerPolicy="no-referrer"
                style={{
                  opacity: 0, // ซ่อน B
                  transition: "opacity 80ms linear",
                  objectFit,
                }}
              />
            </>
          )}

          <div className="video-controls">
            <button
              onClick={togglePlayPause}
              className="play-pause-button"
              aria-label={isPlaying ? "Pause video" : "Play video"}
              title={isPlaying ? "Pause" : "Play"}
            >
              <img
                src={
                  process.env.PUBLIC_URL +
                  (isPlaying ? "/icon/Pausevideo.png" : "/icon/Playvideo.png")
                }
                alt=""
                className="video-icon"
                draggable="false"
              />
            </button>
          </div>

          {(hint || mixedBlocked || (useSnapshot && snapshotUnstable)) && (
            <div className="video-hint">
              {mixedBlocked && (
                <>
                  Stream URL is blocked by mixed-content → using snapshot via
                  proxy.
                  <br />
                </>
              )}
              {hint && (
                <>
                  {hint}
                  <br />
                </>
              )}
              {useSnapshot && snapshotUnstable && (
                <>
                  Snapshot loading is slow/unstable. URL:{" "}
                  <code>{baseSnapshotUrl}</code>
                </>
              )}
            </div>
          )}
        </div>

        <div className="video-progress-wrap">
          <PrintProgress
            estimatedSeconds={estimatedSeconds}
            startedAt={startedAt}
            state={state}
          />
        </div>
      </div>

      <PrintControls
        printerId={PRINTER_ID}
        job={job}
        queueNumber={queueNumber}
      />
    </div>
  );
}
