import React, { useEffect, useMemo, useRef, useState } from "react";
import "./PrintProgress.css";

/**
 * PrintProgress
 * props:
 * - estimatedSeconds : เวลารวมที่จะพิมพ์เสร็จ (วินาที)
 * - startedAt        : เวลาเริ่มพิมพ์ (Date, ISO string, หรือ timestamp ms)
 * - state            : "printing" | "paused" | "completed" | "idle"
 * - elapsedSecondsInit (optional)
 * - nowProvider (optional)
 */
export default function PrintProgress({
  estimatedSeconds,
  startedAt,
  state = "printing",
  elapsedSecondsInit = 0,
  nowProvider,
  progressPct,
}) {
  const [elapsed, setElapsed] = useState(elapsedSecondsInit);
  const tickingRef = useRef(null);

  const total = Math.max(Number(estimatedSeconds || 0), 0);

  const startMs = useMemo(() => {
    if (!startedAt) return null;
    if (startedAt instanceof Date) return startedAt.getTime();
    if (typeof startedAt === "string") return Date.parse(startedAt);
    if (typeof startedAt === "number") return startedAt;
    return null;
  }, [startedAt]);

  useEffect(() => {
    if (tickingRef.current) clearInterval(tickingRef.current);
    if (state === "printing" && total > 0 && startMs) {
      tickingRef.current = setInterval(() => {
        const nowMs = typeof nowProvider === "function" ? nowProvider() : Date.now();
        const elapsedSec = Math.max(0, Math.floor((nowMs - startMs) / 1000));
        setElapsed(elapsedSec);
      }, 1000);
    }
    return () => { if (tickingRef.current) clearInterval(tickingRef.current); };
  }, [state, total, startMs, nowProvider]);

  const clampedElapsed = state === "completed" ? total : Math.min(elapsed, total);
  const remaining = Math.max(0, total - clampedElapsed);
  const computedPct = total > 0 ? (clampedElapsed / total) * 100 : 0;
  const percent =
    typeof progressPct === "number" && isFinite(progressPct)
      ? Math.max(0, Math.min(100, progressPct))
      : computedPct;
  const fmt = (sec) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  };

  const statusText =
    state === "paused" ? "Paused" :
    state === "completed" ? "Completed" :
    state === "idle" ? "Available" :
    `${Math.round(percent)}%`;

  return (
    <div className="print-progress">
      <div className="print-progress__header">
        <span className="print-progress__percent">{statusText}</span>
        <span className="print-progress__eta">Remaining Time: {fmt(remaining)}</span>
      </div>
      <div className="print-progress__bar" role="progressbar"
           aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}>
        <div className="print-progress__bar-fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
