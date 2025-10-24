// src/PrintJobInfo.js
import React, { useEffect, useMemo, useState } from "react";
import "./PrintJobInfo.css";
import { useApi } from "./api";
import { useAuth } from "./auth/AuthContext";

/**
 * การ์ดแสดง “งานที่กำลังพิมพ์ตอนนี้”
 * - พยายามใช้ /api/printers/:id/current-job เป็นหลัก
 * - หากว่าง ตกไปใช้ /api/queue/current?printer_id=:id
 * - ทำ URL รูปจาก preview_key / gcode_key (+token) หากไม่มี thumbnail_url
 */
export default function PrintJobInfo({
  printerId = "prusa-core-one",
  job,              // ถ้าส่งมาก็ใช้ทันที
  pollMs = 4000,    // โพลเร็วขึ้นนิดเพื่อให้ทันกับ Queue
  onError,
}) {
  const api = useApi();
  const { token } = useAuth();

  const [data, setData] = useState(job || null);
  const [status, setStatus] = useState(job ? "success" : "idle"); // idle | loading | success | error

  const endpointCJ = useMemo(
    () => `/api/printers/${encodeURIComponent(printerId)}/current-job`,
    [printerId]
  );
  const endpointQueue = useMemo(
    () => `/api/queue/current?printer_id=${encodeURIComponent(printerId)}`,
    [printerId]
  );

  // helper: ทำ URL /files/raw พร้อม token เพื่อให้ <img> โหลดผ่าน auth ได้
  const toRawUrl = (key) => {
    if (!key) return null;
    const u = new URL((api.API_BASE || "") + `/files/raw`, window.location.origin);
    u.searchParams.set("object_key", key);
    if (token) u.searchParams.set("token", token);
    // ถ้า API_BASE ไม่มี ให้ใช้ path แบบสัมพัทธ์
    return api.API_BASE ? u.toString() : `/api/files/raw?object_key=${encodeURIComponent(key)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  };
  const derivePreviewKey = (gk) =>
    gk ? String(gk).replace(/\.(gcode|gco|gc)$/i, ".preview.png") : null;

  // โพล current-job → ถ้าว่าง ตกไป queue/current
  useEffect(() => {
    if (job) return;

    let aborted = false;
    let timer;

    const fetchOnce = async () => {
      try {
        setStatus((s) => (s === "idle" ? "loading" : s));

        // 1) current-job
        let cj = null;
        try {
          cj = await api.get(endpointCJ, {}, { timeoutMs: 8000 });
        } catch (_) {
          cj = null;
        }

        // 2) fallback queue/current
        let payload = cj;
        if (!payload || Object.keys(payload || {}).length === 0) {
          try {
            payload = await api.get(endpointQueue, {}, { timeoutMs: 8000 });
          } catch (_) {
            // ignore
          }
        }

        if (aborted) return;

        if (payload && Object.keys(payload).length > 0) {
          setData(payload);
          setStatus("success");
        } else {
          // ไม่มีงาน -> เคสว่าง
          setData(null);
          setStatus("success");
        }
      } catch (err) {
        if (!aborted) {
          setStatus("error");
          onError?.(err);
        }
      } finally {
        if (!aborted && pollMs > 0) timer = setTimeout(fetchOnce, pollMs);
      }
    };

    fetchOnce();
    return () => { aborted = true; if (timer) clearTimeout(timer); };
  }, [api, endpointCJ, endpointQueue, pollMs, job, onError]);

  // ---- map fields ให้แน่ใจว่าได้ค่าเหมือนหน้า Queue ----
  const mapped = useMemo(() => {
    const d = data || {};
    const queueNumber =
      d.queueNumber ?? d.queue_number ?? d.number ?? d.no ?? "-";

    const fileName =
      d.fileName ?? d.file_name ?? d.name ?? "File Name";

    const previewKey =
      d.preview_key ?? d.previewKey ?? d.manifest?.preview_key ?? null;

    const gcodeKey =
      d.gcode_key ?? d.gcodeKey ?? d.manifest?.gcode_key ?? d.object_key ?? null;

    // เลือกรูป: thumbnail_url -> preview_key -> derive from gcode_key -> placeholder
    let thumb =
      d.thumbnailUrl ??
      d.thumbnail_url ??
      (previewKey ? toRawUrl(previewKey) : null) ??
      (gcodeKey ? toRawUrl(derivePreviewKey(gcodeKey)) : null) ??
      "/images/placeholder-model.png";

    // ขัดเกลา: ถ้า backend ส่ง absolute http(s) มาก็ใช้ตามนั้น
    const s = String(thumb || "");
    if (s.startsWith("http://") || s.startsWith("https://") || s.startsWith("data:")) {
      thumb = s;
    }

    return { queueNumber, fileName, thumb };
  }, [data, token]); // token เปลี่ยนต้องรีคอมพิวท์ thumb

  return (
    <div className="printjob-wrap">
      <div className="pj-divider" aria-hidden />
      <div className="printjob">
        {status === "loading" && !data ? (
          <div className="pj-skeleton">
            <div className="pj-thumb-skeleton" />
            <div className="pj-lines">
              <div className="pj-line w50" />
              <div className="pj-line w80" />
            </div>
          </div>
        ) : status === "error" && !data ? (
          <div className="pj-error">ไม่พบข้อมูลงานพิมพ์</div>
        ) : data ? (
          <div className="pj-content">
            <img
              className="pj-thumb"
              src={mapped.thumb}
              alt={mapped.fileName || "3D model"}
              onError={(e) => { e.currentTarget.src = "/images/placeholder-model.png"; }}
            />
            <div className="pj-meta">
              <div className="pj-queue">{String(mapped.queueNumber).padStart(3, "0")}</div>
              <div className="pj-name">{mapped.fileName}</div>
            </div>
          </div>
        ) : (
          // เคสไม่มีงานกำลังพิมพ์
          <div className="pj-content">
            <img className="pj-thumb" src="/images/placeholder-model.png" alt="" />
            <div className="pj-meta">
              <div className="pj-queue">—</div>
              <div className="pj-name">No active job</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
