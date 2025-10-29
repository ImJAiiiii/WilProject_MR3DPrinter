import React, { useMemo, useState, useEffect } from "react";
import "./PrintControls.css";
import PrintJobInfo from "./PrintJobInfo";

/* ===== โมดัลยืนยันยกเลิก (prefix cc- เพื่อไม่ชนสไตล์เดิม) ===== */
function ConfirmCancelModal({ open, onClose, onConfirm, code = "003", name = "Part Name" }) {
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
  job,                 // { name, thumb } | null
  queueNumber,         // "001" | null
  state,               // (optional) 'printing' | 'paused' | 'ready' | 'error' | 'offline' | 'idle'
  onAfterAction,       // (optional) callback เรียกหลัง pause/cancel สำเร็จ
}) {
  // ===== Config/API =====
  const API_BASE = useMemo(() => process.env.REACT_APP_API_BASE || "", []);
  const token = useMemo(
    () => (typeof window !== "undefined" ? localStorage.getItem("token") : null),
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
  const [message, setMessage] = useState(null); // ข้อความสั้นๆ หลังสั่งงาน

  // ===== Actions → Backend =====
  const apiPost = async (path, body) => {
    if (!API_BASE) throw new Error("REACT_APP_API_BASE is not set");
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: body ? JSON.stringify(body) : null,
    });
    const isJson = res.headers.get("content-type")?.includes("application/json");
    const data = isJson ? await res.json().catch(() => ({})) : {};
    if (!res.ok) {
      const detail = data?.detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  };

  // Pause/Resume: ถ้าระบุ state เข้ามาและเป็น paused ให้ตีความเป็น resume
  const pausedLike = String(state || "").toLowerCase().includes("pause");

  const handlePause = async () => {
  try {
    setMessage(null);
    setIsPausing(true);

    // ✅ ใช้ toggle ตลอด → ถ้ากำลังพิมพ์จะ Pause, ถ้ากำลัง Paused จะ Resume
    await apiPost(`/printers/${encodeURIComponent(printerId)}/octoprint/command`, {
      command: "pause",
      action: "toggle",
    });

    // แค่ข้อความบอกผู้ใช้ (อิงจาก label เดิมที่เราคาดไว้)
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
      // ใช้ wrapper สั้น ๆ ที่เราเพิ่มใน backend แล้ว
      await apiPost(`/printers/${encodeURIComponent(printerId)}/cancel`);
      setMessage("Canceled");
      onAfterAction?.("cancel");
    } catch (e) {
      console.error(e);
      setMessage(`Failed: ${e.message || e}`);
      // fallback ยิงผ่าน /octoprint/command
      try {
        await apiPost(`/printers/${encodeURIComponent(printerId)}/octoprint/command`, {
          command: "cancel",
        });
      } catch {}
    } finally {
      setIsCanceling(false);
      setTimeout(() => setMessage(null), 3000);
    }
  };

  // ✅ ใช้ข้อมูลจริงจาก props (fallback ถ้าไม่มี)
  const previewSrc = job?.thumb || "/images/3D.png";
  const displayName = job?.name || "File Name";
  const displayQueue = queueNumber || "001";

  // ป้ายสถานะใต้ปุ่ม (เล็กๆ)
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
          <img className="icon-hover"   src="/icon/pauseprinthover.png" alt="" />
          <img className="icon-active"  src="/icon/pauseprintselect.png" alt="" />
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
          <img className="icon-hover"   src="/icon/cancelhover.png" alt="" />
          <img className="icon-active"  src="/icon/cancelselect.png" alt="" />
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
