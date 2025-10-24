// src/PausePrintButton.js
import React, { useState } from "react";
import "./PrintControls.css";
import { useApi } from './api/index';

export default function PausePrintButton({ printerId, onPaused }) {
  // idle | loading | paused | error
  const [status, setStatus] = useState("idle");
  const { post } = useApi();     // ✅ ใช้ post จาก hook

  const handlePause = async () => {
    if (status === "loading") return;
    setStatus("loading");
    try {
      // ✅ เปลี่ยนจาก fetch → useApi.post (แนบ Bearer ให้อัตโนมัติ + จัดการ 401)
      await post(`/api/printers/${printerId}/pause`);
      setStatus("paused");
      onPaused?.();
    } catch (err) {
      console.error(err);
      setStatus("error");
      setTimeout(() => setStatus("idle"), 1500);
    }
  };

  return (
    <button
      className={`pause-print-btn ${status}`}
      onClick={handlePause}
      aria-label="Pause print"
      title="Pause"
    >
      <img src="/icon/pauseprint.png" alt="" draggable="false" />
      <span className="btn-label">Pause</span>
    </button>
  );
}
