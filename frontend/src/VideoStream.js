// src/VideoStream.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import "./VideoStream.css";
import PrintProgress from "./PrintProgress";
import PrintControls from "./PrintControls";
import { API_BASE } from "./api";

const PRINTER_ID = process.env.REACT_APP_PRINTER_ID || "prusa-core-one";

// STREAM_URL: ถ้ามีก็เลือกโหมดจาก REACT_APP_STREAM_MODE = "mjpeg" | "video"
// ไม่ตั้ง → เดาให้: มี "action=stream" => mjpeg, ไม่งั้น video
const STREAM_URL = (process.env.REACT_APP_STREAM_URL || "").trim();
const STREAM_MODE_ENV = (process.env.REACT_APP_STREAM_MODE || "").trim().toLowerCase();

// snapshot โดยตรง (ถ้าต้องยิงตรง) — ถ้าไม่ใส่ จะใช้ proxy ของ backend
const DIRECT_SNAPSHOT = (process.env.REACT_APP_SNAPSHOT_URL || "").trim();

function decideStreamMode(url, envMode) {
  if (!url) return "snapshot";
  if (envMode === "mjpeg" || envMode === "video") return envMode;
  // เดาอัตโนมัติ
  if (/action=stream|mjpg|mjpeg/i.test(url)) return "mjpeg";
  return "video";
}

export default function VideoStream({
  estimatedSeconds = 0,
  startedAt = null,
  state = "idle",
  job,
  queueNumber,
  // ปรับความลื่น snapshot ได้ที่นี่
  targetFps = 15,
  minFpsHidden = 3,
  // ปรับการยืดภาพหากต้องการ: "contain" | "cover"
  objectFit = "contain",
}) {
  /* ---------------- mode & urls ---------------- */
  const mode = useMemo(() => decideStreamMode(STREAM_URL, STREAM_MODE_ENV), [STREAM_URL, STREAM_MODE_ENV]);
  const isStreamMJPEG = mode === "mjpeg";
  const isStreamVideo = mode === "video";
  const useSnapshot = !STREAM_URL || mode === "snapshot";

  const baseSnapshotUrl = useMemo(
    () =>
      DIRECT_SNAPSHOT ||
      `${API_BASE}/printers/${encodeURIComponent(PRINTER_ID)}/snapshot`,
    [DIRECT_SNAPSHOT, API_BASE, PRINTER_ID]
  );

  /* ---------------- common states ---------------- */
  const [isPlaying, setIsPlaying] = useState(true);

  /* ---------------- video mode ---------------- */
  const videoRef = useRef(null);
  useEffect(() => {
    if (!isStreamVideo) return;
    const el = videoRef.current;
    if (!el) return;

    // reset ก่อนสลับโหมด/URL เพื่อกันเฟรมค้าง
    try {
      el.pause();
      el.removeAttribute("src");
      el.load?.();
    } catch {}

    el.src = STREAM_URL;
    el.muted = true;
    el.playsInline = true;
    el.preload = "none";

    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    const onError = () => setIsPlaying(false);

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
  }, [isStreamVideo, STREAM_URL]);

  /* ---------------- mjpeg mode ---------------- */
  // mjpeg ใช้ <img src=STREAM_URL> ตรง ๆ ลื่นสุด (ไม่ยุ่ง canvas/blob)

  /* ---------------- snapshot (double buffer + adaptive FPS) ---------------- */
  const [snapSrc, setSnapSrc] = useState(""); // src ปัจจุบันที่แสดง
  const errCountRef = useRef(0);
  const runningRef = useRef(false);
  const lastCostRef = useRef(0);

  useEffect(() => {
    if (!useSnapshot) return;
    runningRef.current = true;

    const imgA = new Image();
    const imgB = new Image();
    let cur = imgA;
    let nxt = imgB;

    // ให้ browser ทำงานข้าม-origin แบบนิ่ง ๆ ถ้า backend เปิด CORS รูปไว้
    cur.crossOrigin = "anonymous";
    nxt.crossOrigin = "anonymous";

    const makeUrl = () =>
      `${baseSnapshotUrl}${baseSnapshotUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;

    const schedule = (costMs) => {
      lastCostRef.current = costMs;

      // โหมดลด FPS เมื่อแท็บไม่โฟกัส
      const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
      const wantFps = hidden ? Math.max(minFpsHidden, Math.floor(targetFps / 3)) : targetFps;

      const frameBudget = Math.max(0, 1000 / wantFps - costMs);
      const id = setTimeout(() => {
        if (runningRef.current && isPlaying) {
          // ใช้ rAF เพื่อให้ sync กับเฟรมเรนเดอร์
          requestAnimationFrame(tick);
        }
      }, frameBudget);
      // กัน memory leak หากถูก unmount ระหว่าง timeout
      schedule._t = id;
    };

    const tick = () => {
      if (!runningRef.current) return;
      const t0 = performance.now();

      nxt.onload = () => {
        // อัปเดต src ที่โชว์ แล้วสลับ buffer
        setSnapSrc(nxt.src);

        // สลับ
        const t1 = performance.now();
        const cost = t1 - t0;

        const tmp = cur;
        cur = nxt;
        nxt = tmp;

        // reset error
        errCountRef.current = 0;

        // เตรียมรอบต่อไป
        schedule(cost);
      };

      nxt.onerror = () => {
        errCountRef.current = Math.min(99, errCountRef.current + 1);
        // backoff เล็กน้อยเมื่อ error
        const id = setTimeout(() => {
          if (runningRef.current && isPlaying) requestAnimationFrame(tick);
        }, 300);
        tick._t = id;
      };

      // กัน cache
      nxt.src = makeUrl();
    };

    // เริ่ม
    if (isPlaying) requestAnimationFrame(tick);

    // cleanup
    return () => {
      runningRef.current = false;
      // ยกเลิก timer ที่ยังค้าง
      if (schedule._t) clearTimeout(schedule._t);
      if (tick._t) clearTimeout(tick._t);
      // ตัด handler กันเก็บอ้างอิงไม่จำเป็น
      cur.onload = cur.onerror = null;
      nxt.onload = nxt.onerror = null;
    };
  }, [useSnapshot, baseSnapshotUrl, isPlaying, targetFps, minFpsHidden]);

  /* ---------------- controls ---------------- */
  const togglePlayPause = async () => {
    if (isStreamVideo) {
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

  return (
    <div className="video-stream">
      <div className="video-block">
        <div className="video-wrapper">
          {isStreamVideo ? (
            <video
              ref={videoRef}
              className="video-el"
              autoPlay
              muted
              playsInline
              preload="none"
              style={{ width: "100%", height: "100%", objectFit }}
            />
          ) : isStreamMJPEG ? (
            <img
              className="video-el"
              alt="live"
              src={STREAM_URL}
              decoding="async"
              fetchpriority="high"
              referrerPolicy="no-referrer"
              style={{ width: "100%", height: "100%", objectFit }}
            />
          ) : (
            <img
              className="video-el"
              alt="Printer camera snapshot"
              // ใส่ src เมื่อมีเท่านั้น กัน src="" (บางเบราว์เซอร์จะยิงรีเควสต์ซ้ำ)
              {...(snapSrc ? { src: snapSrc } : {})}
              decoding="async"
              fetchpriority="high"
              referrerPolicy="no-referrer"
              style={{ width: "100%", height: "100%", objectFit }}
            />
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

          {/* snapshot โชว์ hint เมื่อ error ติดต่อกันเยอะ ๆ */}
          {useSnapshot && errCountRef.current >= 3 && (
            <div className="video-hint">
              โหลดภาพจากกล้องช้า/ขาดช่วง
              <br />
              URL: <code>{baseSnapshotUrl}</code>
              <br />
              (แนะนำลดความละเอียด/เฟรมเรตที่กล้อง หรือใช้โหมด MJPEG ถ้ามี)
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
