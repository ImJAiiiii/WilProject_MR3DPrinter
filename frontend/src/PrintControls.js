// src/PrintControls.js
import React, { useMemo, useState, useEffect } from "react";
import "./PrintControls.css";

/* ===== โมดัลยืนยันยกเลิก (prefix cc- เพื่อไม่ชนสไตล์เดิม) ===== */
function ConfirmCancelModal({
  open,
  onClose,
  onConfirm,
  code = "003",
  name = "Part Name",
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="cc-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="cc-modal" onClick={(e) => e.stopPropagation()}>
        <h2 className="cc-title">Cancel printing this part?</h2>
        <div className="cc-part">
          <span className="cc-code">{code}</span>&nbsp;
          <span className="cc-name">[{name}]</span>
        </div>

        <div className="cc-actions">
          <button type="button" className="cc-btn cc-ghost" onClick={onClose}>
            Go Back
          </button>
          <button
            type="button"
            className="cc-btn cc-danger"
            onClick={() => {
              onConfirm?.();
              onClose();
            }}
          >
            Yes, Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

export default function PrintControls({
  printerId = "prusa-core-one",
  job,           // { name, thumb } | null (fallback)
  queueNumber,   // "001" | null    (fallback)
  state,         // 'printing' | 'paused' | 'ready' | 'error' | 'offline' | 'idle'
  onAfterAction, // callback หลัง pause/cancel สำเร็จ
}) {
  // ===== Config/API =====
  const API_BASE = useMemo(() => process.env.REACT_APP_API_BASE || "", []);
  const token = useMemo(
    () =>
      typeof window !== "undefined" ? localStorage.getItem("token") : null,
    []
  );
  const headers = useMemo(() => {
    const h = { "Content-Type": "application/json" };
    if (token) h["Authorization"] = `Bearer ${token}`;
    return h;
  }, [token]);

  // ===== UI state =====
  const [isPausing, setIsPausing] = useState(false);
  const [isCanceling, setIsCanceling] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [message, setMessage] = useState(null);

  // ===== current-job จาก backend (map ให้เป็น shape เดียวกันเสมอ) =====
  // normalized: { name, thumb, queueNumber }
  const [jobFromApi, setJobFromApi] = useState(null);

  useEffect(() => {
    if (!API_BASE) return;
    let cancelled = false;

    // helper แปลง response จาก backend → รูปแบบเดียวกัน
    const normalizeJob = (raw) => {
      if (!raw || typeof raw !== "object") return null;
      const name =
        raw.fileName ??
        raw.file_name ??
        raw.name ??
        null;
      const thumb =
        raw.thumbnailUrl ??
        raw.thumbnail_url ??
        raw.thumb ??
        null;
      const queueNumber =
        raw.queueNumber ??
        raw.queue_number ??
        null;

      return { name, thumb, queueNumber };
    };

    const fetchCurrentJob = async () => {
      try {
        const res = await fetch(
          `${API_BASE}/api/printers/${encodeURIComponent(
            printerId
          )}/current-job`,
          { headers }
        );
        if (!res.ok) {
          // 404 = ไม่มีงาน active → เคลียร์แล้วจบ
          if (!cancelled && res.status === 404) {
            setJobFromApi(null);
          }
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setJobFromApi(normalizeJob(data));
      } catch (e) {
        if (!cancelled) {
          console.error("fetch current-job failed", e);
        }
      }
    };

    // ดึงครั้งแรก
    fetchCurrentJob();
    // แล้วดึงวนทุก 8 วินาที
    const iv = setInterval(fetchCurrentJob, 8000);

    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [API_BASE, headers, printerId]);

  // ===== ฟังก์ชันยิง API POST =====
  const apiPost = async (path, body) => {
    if (!API_BASE) throw new Error("REACT_APP_API_BASE is not set");
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: body ? JSON.stringify(body) : null,
    });
    const isJson = res.headers
      .get("content-type")
      ?.includes("application/json");
    const data = isJson ? await res.json().catch(() => ({})) : {};
    if (!res.ok) {
      const detail = data?.detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  };

  // Pause/Resume: ถ้า state มีคำว่า "pause" → ถือว่าเป็น paused
  const pausedLike = String(state || "").toLowerCase().includes("pause");

  const handlePause = async () => {
    try {
      setMessage(null);
      setIsPausing(true);

      // toggle pause/resume ที่ OctoPrint
      await apiPost(
        `/printers/${encodeURIComponent(printerId)}/octoprint/command`,
        {
          command: "pause",
          action: "toggle",
        }
      );

      setMessage(pausedLike ? "Resumed" : "Paused");
      onAfterAction?.("pause");
    } catch (e) {
      console.error(e);
      setMessage(`Failed: ${e.message || e}`);
    } finally {
      setIsPausing(false);
      setTimeout(() => setMessage(null), 3000);
    }
  };

  const handleCancelClick = () => setShowConfirm(true);

  const performCancel = async () => {
    try {
      setMessage(null);
      setIsCanceling(true);
      await apiPost(`/printers/${encodeURIComponent(printerId)}/cancel`);
      setMessage("Canceled");
      onAfterAction?.("cancel");
    } catch (e) {
      console.error(e);
      setMessage(`Failed: ${e.message || e}`);
      // fallback ยิง cancel ตรงไปที่ OctoPrint
      try {
        await apiPost(
          `/printers/${encodeURIComponent(printerId)}/octoprint/command`,
          { command: "cancel" }
        );
      } catch {
        /* ignore */
      }
    } finally {
      setIsCanceling(false);
      setTimeout(() => setMessage(null), 3000);
    }
  };

  // ================= ใช้ข้อมูลจริงสำหรับแสดงผล =================

  // ให้ current-job จาก backend มี priority สูงสุด
  const effectiveJob = jobFromApi
    ? {
        name: jobFromApi.name,
        thumb: jobFromApi.thumb,
      }
    : job || null;

  const effectiveQueueNumber = jobFromApi?.queueNumber != null
    ? String(jobFromApi.queueNumber).padStart(3, "0")
    : queueNumber || "001";

  const previewSrc = effectiveJob?.thumb || "/icon/noimage.png";
  const displayName = effectiveJob?.name || "File Name";
  const displayQueue = effectiveQueueNumber;

  const ActionHint = () =>
    message ? (
      <div className="pc-hint" role="status" aria-live="polite">
        {message}
      </div>
    ) : null;

  return (
    <div className="under-progress-row">
      {/* ซ้าย: ปุ่มควบคุม */}
      <div className="print-controls">
        <button
          type="button"
          className={`control-btn pause-btn${isPausing ? " is-busy" : ""}`}
          onClick={handlePause}
          disabled={isPausing}
          aria-label={pausedLike ? "Resume print" : "Pause print"}
          title={pausedLike ? "Resume" : "Pause"}
        >
          <img className="icon-default" src="/icon/pauseprint.png" alt="" />
          <img className="icon-hover" src="/icon/pauseprinthover.png" alt="" />
          <img
            className="icon-active"
            src="/icon/pauseprintselect.png"
            alt=""
          />
          <span className="label">{pausedLike ? "Resume" : "Pause"}</span>
        </button>

        <div className="controls-divider" aria-hidden />

        <button
          type="button"
          className={`control-btn cancel-btn${isCanceling ? " is-busy" : ""}`}
          onClick={handleCancelClick}
          disabled={isCanceling}
          aria-label="Cancel print"
          title="Cancel"
        >
          <img className="icon-default" src="/icon/cancelprint.png" alt="" />
          <img className="icon-hover" src="/icon/cancelhover.png" alt="" />
          <img className="icon-active" src="/icon/cancelselect.png" alt="" />
          <span className="label">Cancel</span>
        </button>

        <ActionHint />
      </div>

      {/* ขวา: การ์ดข้อมูลไฟล์ */}
      <div className="print-job-info">
        <img src={previewSrc} alt={displayName} className="print-job-preview" />
        <div className="print-job-details">
          <div className="print-job-queue">{displayQueue}</div>
          <div className="print-job-name">{displayName}</div>
        </div>
      </div>

      {/* โมดัลยืนยัน */}
      <ConfirmCancelModal
        open={showConfirm}
        onClose={() => setShowConfirm(false)}
        onConfirm={performCancel}
        code={displayQueue}
        name={displayName}
      />
    </div>
  );
}