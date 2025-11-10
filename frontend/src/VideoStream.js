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
  // ปรับภาพให้พอดี
  objectFit = "contain",
  // ถ้าไม่ได้ตั้ง env จะใช้ค่าดีฟอลต์นี้
  targetFps = Number.isFinite(SNAPSHOT_FPS_ENV) && SNAPSHOT_FPS_ENV > 0 ? SNAPSHOT_FPS_ENV : 8,
  minFpsHidden = 3,
}) {
  // ---------- mode/url ----------
  const streamUrl = STREAM_URL_RAW || "";
  const mode = useMemo(() => decideStreamMode(streamUrl, STREAM_MODE_ENV), [streamUrl]);
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
  }, []);

  const useSnapshot = !streamUrl || (streamUrl && !isSameSecureScheme(streamUrl));

  // ---------- states ----------
  const [isPlaying, setIsPlaying] = useState(true);

  // ---------- <video> (ไม่แตะ ส่วนนี้เดิม) ----------
  const videoRef = useRef(null);
  useEffect(() => {
    if (!isStreamVideo || useSnapshot) return;
    const el = videoRef.current;
    if (!el) return;

    try { el.pause(); el.removeAttribute("src"); el.load?.(); } catch {}
    el.src = streamUrl;
    el.muted = true;
    el.playsInline = true;
    el.preload = "none";

    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    const onError = () => { setIsPlaying(false); setHint("Stream error → snapshot fallback"); };

    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("error", onError);

    (async () => { try { await el.play(); } catch { setIsPlaying(false); } })();

    return () => {
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("error", onError);
      try { el.pause(); el.removeAttribute("src"); el.load?.(); } catch {}
    };
  }, [isStreamVideo, streamUrl, useSnapshot]);

  // ---------- Snapshot: dual visible buffers (no flicker) ----------
  const [srcA, setSrcA] = useState("");
  const [srcB, setSrcB] = useState("");
  const [showA, setShowA] = useState(true); // true = แสดง A, false = แสดง B

  const errCountRef = useRef(0);
  const runningRef = useRef(false);

  useEffect(() => {
    if (!useSnapshot) return;
    runningRef.current = true;

    // offscreen loaders
    const loaderA = new Image();
    const loaderB = new Image();
    loaderA.crossOrigin = loaderB.crossOrigin = "anonymous";

    let active = "A"; // กำลังโหลดภาพเข้าบัฟเฟอร์ไหน (ตรงข้ามกับที่โชว์)
    let timerId = null;

    const makeUrl = () =>
      `${baseSnapshotUrl}${baseSnapshotUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;

    const schedule = (delayMs) => {
      timerId = setTimeout(() => {
        if (runningRef.current && isPlaying) requestAnimationFrame(tick);
      }, delayMs);
    };

    const tick = () => {
      if (!runningRef.current) return;

      const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
      const wantFps = hidden ? Math.max(minFpsHidden, Math.floor(targetFps / 3)) : targetFps;
      const budget = Math.max(0, 1000 / Math.max(1, wantFps));

      const target = active === "A" ? loaderA : loaderB;
      target.onload = () => {
        if (!runningRef.current) return;

        if (active === "A") {
          setSrcA(target.src);
          setShowA(true);   // โชว์ A, ซ่อนไว้ B
          active = "B";     // รอบหน้าวนไปโหลด B
        } else {
          setSrcB(target.src);
          setShowA(false);  // โชว์ B
          active = "A";
        }

        errCountRef.current = 0;
        schedule(budget);
      };

      target.onerror = () => {
        errCountRef.current = Math.min(99, errCountRef.current + 1);
        schedule(Math.max(300, budget)); // backoff เล็กน้อย
      };

      target.src = makeUrl();
    };

    if (isPlaying) requestAnimationFrame(tick);

    return () => {
      runningRef.current = false;
      if (timerId) clearTimeout(timerId);
      loaderA.onload = loaderA.onerror = null;
      loaderB.onload = loaderB.onerror = null;
    };
  }, [useSnapshot, baseSnapshotUrl, isPlaying, targetFps, minFpsHidden]);

  // ---------- controls ----------
  const togglePlayPause = async () => {
    if (isStreamVideo && !useSnapshot) {
      const el = videoRef.current;
      if (!el) return;
      if (el.paused || el.ended) {
        try { await el.play(); setIsPlaying(true); } catch {}
      } else {
        el.pause(); setIsPlaying(false);
      }
    } else {
      setIsPlaying(v => !v);
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
              style={{ width: "100%", height: "100%", objectFit }}
            />
          ) : isStreamMJPEG && !useSnapshot ? (
            <img
              className="video-el"
              alt="live"
              src={streamUrl}
              decoding="async"
              fetchpriority="high"
              referrerPolicy="no-referrer"
              style={{ width: "100%", height: "100%", objectFit }}
            />
          ) : (
            <>
              {/* สองบัฟเฟอร์ “บนจอ” สลับ opacity → ไม่กะพริบ */}
              <img
                alt=""
                decoding="async"
                fetchpriority="high"
                referrerPolicy="no-referrer"
                src={srcA || undefined}
                className="video-el"
                style={{
                  position: "absolute",
                  inset: 0,
                  width: "100%",
                  height: "100%",
                  objectFit,
                  transition: "opacity 80ms linear",
                  opacity: showA ? 1 : 0,
                  willChange: "opacity",
                  backfaceVisibility: "hidden",
                }}
              />
              <img
                alt="Printer camera snapshot"
                decoding="async"
                fetchpriority="high"
                referrerPolicy="no-referrer"
                src={srcB || undefined}
                className="video-el"
                style={{
                  position: "absolute",
                  inset: 0,
                  width: "100%",
                  height: "100%",
                  objectFit,
                  transition: "opacity 80ms linear",
                  opacity: showA ? 0 : 1,
                  willChange: "opacity",
                  backfaceVisibility: "hidden",
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

          {(hint || mixedBlocked || (useSnapshot && errCountRef.current >= 3)) && (
            <div className="video-hint">
              {mixedBlocked && <>Stream URL is blocked by mixed-content → using snapshot via proxy.<br/></>}
              {hint && <>{hint}<br/></>}
              {useSnapshot && errCountRef.current >= 3 && (
                <>Snapshot loading is slow/unstable. URL: <code>{baseSnapshotUrl}</code></>
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

      <PrintControls printerId={PRINTER_ID} job={job} queueNumber={queueNumber} />
    </div>
  );
}
