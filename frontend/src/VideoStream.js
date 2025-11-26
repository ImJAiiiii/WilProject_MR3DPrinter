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
<<<<<<< Updated upstream
  // ปรับความลื่น snapshot ได้ที่นี่
  targetFps = 15,
=======
  objectFit = "cover", // ให้เต็มเฟรมเลย
  targetFps =
    Number.isFinite(SNAPSHOT_FPS_ENV) && SNAPSHOT_FPS_ENV > 0
      ? SNAPSHOT_FPS_ENV
      : 8,
>>>>>>> Stashed changes
  minFpsHidden = 3,
  // ปรับการยืดภาพหากต้องการ: "contain" | "cover"
  objectFit = "contain",
}) {
<<<<<<< Updated upstream
  /* ---------------- mode & urls ---------------- */
  const mode = useMemo(() => decideStreamMode(STREAM_URL, STREAM_MODE_ENV), [STREAM_URL, STREAM_MODE_ENV]);
=======
  // ---------- mode/url ----------
  const streamUrl = STREAM_URL_RAW || "";
  const mode = useMemo(
    () => decideStreamMode(streamUrl, STREAM_MODE_ENV),
    [streamUrl]
  );
>>>>>>> Stashed changes
  const isStreamMJPEG = mode === "mjpeg";
  const isStreamVideo = mode === "video";
  const useSnapshot = !STREAM_URL || mode === "snapshot";

<<<<<<< Updated upstream
  const baseSnapshotUrl = useMemo(
    () =>
      DIRECT_SNAPSHOT ||
      `${API_BASE}/printers/${encodeURIComponent(PRINTER_ID)}/snapshot`,
    [DIRECT_SNAPSHOT, API_BASE, PRINTER_ID]
  );

  /* ---------------- common states ---------------- */
  const [isPlaying, setIsPlaying] = useState(true);

  /* ---------------- video mode ---------------- */
=======
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
>>>>>>> Stashed changes
  const videoRef = useRef(null);
  useEffect(() => {
    if (!isStreamVideo) return;
    const el = videoRef.current;
    if (!el) return;

<<<<<<< Updated upstream
    // reset ก่อนสลับโหมด/URL เพื่อกันเฟรมค้าง
=======
>>>>>>> Stashed changes
    try {
      el.pause();
      el.removeAttribute("src");
      el.load?.();
    } catch {}

<<<<<<< Updated upstream
    el.src = STREAM_URL;
=======
    el.src = streamUrl;
>>>>>>> Stashed changes
    el.muted = true;
    el.playsInline = true;
    el.preload = "none";

    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
<<<<<<< Updated upstream
    const onError = () => setIsPlaying(false);
=======
    const onError = () => {
      setIsPlaying(false);
      setHint("Stream error → snapshot fallback");
    };
>>>>>>> Stashed changes

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

<<<<<<< Updated upstream
  /* ---------------- mjpeg mode ---------------- */
  // mjpeg ใช้ <img src=STREAM_URL> ตรง ๆ ลื่นสุด (ไม่ยุ่ง canvas/blob)

  /* ---------------- snapshot (double buffer + adaptive FPS) ---------------- */
  const [snapSrc, setSnapSrc] = useState(""); // src ปัจจุบันที่แสดง
=======
  // ---------- Snapshot: double-buffer DOM (no React state per frame) ----------
  const imgARef = useRef(null);
  const imgBRef = useRef(null);
  const [snapshotUnstable, setSnapshotUnstable] = useState(false);
>>>>>>> Stashed changes
  const errCountRef = useRef(0);
  const runningRef = useRef(false);
  const lastCostRef = useRef(0);

  useEffect(() => {
    if (!useSnapshot) return;

    runningRef.current = true;
    errCountRef.current = 0;
    setSnapshotUnstable(false);

<<<<<<< Updated upstream
    const imgA = new Image();
    const imgB = new Image();
    let cur = imgA;
    let nxt = imgB;

    // ให้ browser ทำงานข้าม-origin แบบนิ่ง ๆ ถ้า backend เปิด CORS รูปไว้
    cur.crossOrigin = "anonymous";
    nxt.crossOrigin = "anonymous";
=======
    let visible = "A"; // ตัวไหนโชว์อยู่ตอนนี้ ("A" หรือ "B")
    let destroyed = false;
    let timerId = null;
>>>>>>> Stashed changes

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

<<<<<<< Updated upstream
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
=======
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
>>>>>>> Stashed changes

    // cleanup
    return () => {
      destroyed = true;
      runningRef.current = false;
<<<<<<< Updated upstream
      // ยกเลิก timer ที่ยังค้าง
      if (schedule._t) clearTimeout(schedule._t);
      if (tick._t) clearTimeout(tick._t);
      // ตัด handler กันเก็บอ้างอิงไม่จำเป็น
      cur.onload = cur.onerror = null;
      nxt.onload = nxt.onerror = null;
=======
      if (timerId) clearTimeout(timerId);
      loader.onload = loader.onerror = null;
>>>>>>> Stashed changes
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
            />
          ) : isStreamMJPEG ? (
            <img
              className="video-el"
              alt="live"
              src={STREAM_URL}
              decoding="async"
              fetchpriority="high"
              referrerPolicy="no-referrer"
            />
          ) : (
<<<<<<< Updated upstream
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
=======
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
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream
          {/* snapshot โชว์ hint เมื่อ error ติดต่อกันเยอะ ๆ */}
          {useSnapshot && errCountRef.current >= 3 && (
            <div className="video-hint">
              โหลดภาพจากกล้องช้า/ขาดช่วง
              <br />
              URL: <code>{baseSnapshotUrl}</code>
              <br />
              (แนะนำลดความละเอียด/เฟรมเรตที่กล้อง หรือใช้โหมด MJPEG ถ้ามี)
=======
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
>>>>>>> Stashed changes
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
