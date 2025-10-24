// src/PrinterStatus.js
import React from "react";

/** เหมือน NormalizeMode ใน StatusSync.cs */
function normalizeMode(s = "") {
  const t = String(s).trim().toLowerCase();
  if (t === "printing" || t === "running") return "Printing";
  if (t === "queued") return "Queued";
  if (t === "paused" || t === "hold") return "Paused";
  if (["completed","finished","success","done"].includes(t)) return "Completed";
  if (["cancelling","canceling","cancelled","canceled"].includes(t)) return "Cancelling";
  if (["error","failed","aborted"].includes(t)) return "Error";
  return "Normal";
}

/** สร้างข้อความสถานะ:
 * ออนไลน์  → ใช้ status_text จาก BE (ว่างให้ “Printer is ready”)
 * ออฟไลน์ → ให้ “Waiting for connection”
 */
function buildStatusText(isOnline, statusTextFromBE) {
  return isOnline
    ? (String(statusTextFromBE || "").trim() || "Printer is ready")
    : "Waiting for connection";
}

/** สีของจุดตามสถานะ/โหมด */
function dotColor(online, textOrMode) {
  if (!online) return "#d32f2f"; // แดง = ออฟไลน์
  const s = String(textOrMode || "").toLowerCase();
  if (s.includes("error") || s.includes("fail") || s.includes("cancel")) return "#ff9800"; // ส้ม = ผิดพลาด/ยกเลิก
  if (s.includes("print") || s.includes("busy") || s.includes("processing") || s.includes("queued") || s.includes("pause"))
    return "#2e7d32"; // เขียวเข้ม = กำลังทำงาน/คิว/พัก
  return "#2e7d32"; // เขียว = พร้อมใช้งาน
}

export default function PrinterStatus({
  status,            // raw status_text จาก BE
  printerOnline,     // boolean ออนไลน์ไหม
  mode,              // raw state เช่น printing/paused/queued...
  className = "",
}) {
  const normMode = normalizeMode(mode || status);
  const uiText = buildStatusText(printerOnline, status);
  const color = dotColor(printerOnline, normMode || uiText);

  return (
    <div className={`printer-status ${className}`} role="status" aria-live="polite">
      <div style={{ display: "flex", alignItems: "center", color: "#111" }}>
        <span
          aria-hidden
          style={{
            width: 10,
            height: 10,
            borderRadius: "50%",
            backgroundColor: color,
            display: "inline-block",
            marginRight: 8,
          }}
        />
        {/* ตัวอย่างผลลัพธ์: Offline | Waiting for connection  หรือ  Online | Printer is ready */}
        <span>{printerOnline ? "Online" : "Offline"} | {uiText}</span>
      </div>
    </div>
  );
}
