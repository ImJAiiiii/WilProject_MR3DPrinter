// src/pages/PrintingPage.js
import React, { useEffect, useMemo, useState, useCallback } from "react";
import "./PrintingPage.css";
import UserPrintHistoryModal from "../UserPrintHistoryModal";
import { useApi } from "../api";
import { useAuth } from "../auth/AuthContext";

export default function PrintingPage({
  jobs = [],
  currentUserId,
  currentRemainingSeconds, // เวลาเหลือจริงจาก OctoPrint สำหรับ “งานที่กำลังพิมพ์”
  onCancelJob,             // ถ้ามี App ภายนอกจะยิง API และ refresh ให้เอง
  onOpenUpload,
  gotoStorage,
  onQueueFromHistory,
  waitMap,                 // (legacy) ไม่ใช้แล้ว แต่ยังรับไว้
  lastQueuedJobId = null,  // ใช้ไฮไลท์รายการที่เพิ่งเข้าคิว
}) {
  const api = useApi();
  const { token } = useAuth(); // ใช้สำหรับแนบกับ /files/raw
  const API_BASE =
    api?.API_BASE ||
    (typeof window !== "undefined" && window.__API_BASE__) ||
    process.env.REACT_APP_API_BASE ||
    "";

  const [fabOpen, setFabOpen] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  // ========== Confirm cancel ==========
  const [confirmJob,   setConfirmJob]   = useState(null);
  const [confirmBusy,  setConfirmBusy]  = useState(false);
  const openConfirm  = (job) => setConfirmJob(job);
  const closeConfirm = () => { if (!confirmBusy) setConfirmJob(null); };

  // fallback: ยกเลิกผ่าน API ตรง ถ้า parent ไม่ได้ส่ง onCancelJob มา
  const cancelViaApi = useCallback(async (job) => {
    const printerId =
      job?.printer_id ||
      job?.printerId ||
      job?.printer?.id ||
      process.env.REACT_APP_PRINTER_ID ||
      "prusa-core-one";
    const jid = job?.id ?? job?.job_id ?? job?.jobId;
    if (!jid) throw new Error("Missing job id");
    await api.queue.cancel(printerId, jid);
  }, [api]);

  const handleConfirmCancel = async () => {
    if (!confirmJob) return;
    try {
      setConfirmBusy(true);
      if (typeof onCancelJob === "function") {
        await onCancelJob(confirmJob.id); // parent refresh ให้
      } else {
        await cancelViaApi(confirmJob);   // fallback ยิงเอง
      }
    } catch {
      // เงียบ ๆ
    } finally {
      setConfirmBusy(false);
      setConfirmJob(null);
    }
  };
  // ====================================

  // ปิด FAB ด้วย Escape
  useEffect(() => {
    if (!fabOpen) return;
    const onKey = (e) => e.key === "Escape" && setFabOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fabOpen]);

  // re-render ทุก 1s เพื่อให้เวลาลดลง
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((v) => (v + 1) % 1e9), 1000);
    return () => clearInterval(id);
  }, []);

  // ฟังอีเวนต์รูปสแนปจากหน้า Preview (จะยิง event: 'queue-thumb')
  useEffect(() => {
    const onThumb = () => force((v) => (v + 1) % 1e9);
    window.addEventListener("queue-thumb", onThumb);
    // เผื่อกรณี snapshot เกิดในอีกแท็บ: ฟัง storage event ด้วย
    const onStorage = (e) => {
      if (typeof e?.key === "string" && e.key.startsWith("queueThumb")) {
        force((v) => (v + 1) % 1e9);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("queue-thumb", onThumb);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // ไฮไลท์งานที่เพิ่งเข้าคิว 6 วินาที
  const [highlightId, setHighlightId] = useState(null);
  useEffect(() => {
    if (!lastQueuedJobId) return;
    setHighlightId(String(lastQueuedJobId));
    const t = setTimeout(() => setHighlightId(null), 6000);
    return () => clearTimeout(t);
  }, [lastQueuedJobId]);

  const pad3 = (n) => String(n).padStart(3, "0");
  const fmtHM = (sec) => {
    if (sec == null) return "—";
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };

  // ตัดนามสกุลไฟล์ออกจากชื่อที่โชว์
  const stripExt = (s) =>
    (s || "").replace(/\.(gcode|gco|g|stl|obj|amf)$/i, "");

  const getStartedMs = (job) => {
    if (job?.startedAt != null) return job.startedAt;
    if (job?.started_at) {
      const t = new Date(job.started_at).getTime();
      return Number.isFinite(t) ? t : undefined;
    }
    return undefined;
  };

  const processingIndex = useMemo(
    () => jobs.findIndex((j) => j.status === "processing"),
    [jobs]
  );

  const FINAL_STATUSES = new Set(["completed", "failed", "canceled"]);

  // เวลาที่เหลือของ “งานหนึ่ง”
  const remainingSecondsForJob = (j, isProcessingRow) => {
    if (!j || FINAL_STATUSES.has(j.status)) return 0;

    // (1) แถวกำลังพิมพ์ → ใช้เวลาจาก OctoPrint ถ้ามี
    if (isProcessingRow && currentRemainingSeconds != null) {
      return Math.max(0, Math.floor(currentRemainingSeconds));
    }

    // (2) เวลาที่เหลือจาก BE (หน่วยนาที)
    if (j?.remaining_min != null) return Math.max(0, Math.floor(j.remaining_min * 60));
    if (j?.remainingMin != null)  return Math.max(0, Math.floor(j.remainingMin  * 60));

    // (3) คำนวณเองจากเวลาเริ่มและ time_min
    const totalMin  = j?.time_min ?? j?.durationMin ?? 0;
    const startedMs = getStartedMs(j);

    if (j?.status === "processing" && startedMs) {
      const elapsed = Math.max(0, Math.floor((Date.now() - startedMs) / 1000));
      return Math.max(0, totalMin * 60 - elapsed);
    }

    // queued/paused → ยังไม่เริ่ม ใช้เวลารวมของมันเอง
    return Math.max(0, totalMin * 60);
  };

  // เวลาที่ต้อง “รอจนจบแถวนี้” (รวมสะสมตั้งแต่หัวตารางถึง index นั้น)
  const waitingOrRemainingOf = (index) => {
    if (!jobs[index]) return "—";
    let totalSec = 0;
    for (let i = 0; i <= index; i++) {
      const j = jobs[i];
      if (!j || FINAL_STATUSES.has(j.status)) continue;
      const isProcRow = i === processingIndex && j.status === "processing";
      totalSec += remainingSecondsForJob(j, isProcRow);
    }
    return fmtHM(totalSec);
  };

  const openUploadSafely = (e) => {
    e?.stopPropagation?.();
    setFabOpen(false);
    requestAnimationFrame(() => onOpenUpload?.());
  };
  const goStorageSafely = (e) => {
    e?.stopPropagation?.();
    setFabOpen(false);
    requestAnimationFrame(() => gotoStorage?.());
  };

  const placeholderImg = process.env.PUBLIC_URL + "/images/placeholder-model.png";
  const fallbackImg = process.env.PUBLIC_URL + "/images/3D.png";
  const onImgError = (e) => {
    e.currentTarget.onerror = null;
    e.currentTarget.src = fallbackImg;
  };

  const statusText = (s) => {
    switch (s) {
      case "processing": return "Processing";
      case "paused":     return "Paused";
      case "queued":     return "Next in line";
      case "completed":  return "Completed";
      case "failed":     return "Failed";
      case "canceled":   return "Canceled";
      default:           return s || "—";
    }
  };

  const statusClassName = (s) => {
    if (s === "processing") return "status processing";
    if (s === "paused")     return "status paused";
    if (s === "queued")     return "status next";
    if (s === "completed")  return "status done";
    if (s === "failed")     return "status failed";
    if (s === "canceled")   return "status canceled";
    return "status";
  };

  // เจ้าของงาน (แสดงใต้ชื่อ)
  const ownerOf = (job) =>
    job?.ownerName ??
    job?.employee_name ??
    job?.uploader_name ??
    job?.uploadedByName ??
    job?.employee_id ??
    "";

  // สิทธิ์ยกเลิก — ถ้า BE ส่ง me_can_cancel มาก็เชื่อมันก่อน
  const canCancelJob = (job, isProcessing) => {
    if (typeof job?.me_can_cancel === "boolean") return job.me_can_cancel;

    // fallback: ยกเลิกได้ถ้าเป็นของเราเอง และไม่ใช่แถวกำลังพิมพ์ และยังไม่จบ
    const uid = String(currentUserId ?? "");
    const isMine =
      (!!uid && String(job?.uploadedBy ?? "") === uid) ||
      (!!uid && String(job?.employee_id ?? "") === uid);
    const isFinal = ["completed", "failed", "canceled"].includes(job?.status);
    return isMine && !isProcessing && !isFinal;
  };

  /* ---------------- helpers สำหรับ URL/เลือกพรีวิว ---------------- */
  function joinUrl(base, path) {
    try {
      const b = String(base || "").trim();
      const p = String(path || "");
      const origin =
        (typeof window !== "undefined" && window.location?.origin) || "";
      return new URL(p, b ? (b.endsWith("/") ? b : b + "/") : origin + "/").toString();
    } catch { return path; }
  }
  function withToken(u, tkn) {
    if (!tkn) return u;
    try { const url = new URL(u); url.searchParams.set("token", tkn); return url.toString(); }
    catch { const sep = u.includes("?") ? "&" : "?"; return `${u}${sep}token=${encodeURIComponent(tkn)}`; }
  }
  // NEW: เติม cache-buster เพื่อเคลียร์ 404 ที่เพิ่งสร้างไฟล์/รีเฟรช
  function addCacheParam(u, tag) {
    try { const url = new URL(u); url.searchParams.set("t", String(tag ?? Date.now())); return url.toString(); }
    catch { const sep = u.includes("?") ? "&" : "?"; return `${u}${sep}t=${encodeURIComponent(tag ?? Date.now())}`; }
  }
  function toRawUrl(objectKey, cacheTag) {
    const path = `/files/raw?object_key=${encodeURIComponent(objectKey)}`;
    const withBase = joinUrl(API_BASE, path);
    const withAuth = withToken(withBase, token);
    return addCacheParam(withAuth, cacheTag);
  }
  function derivePreviewFromGcodeKey(k) {
    if (!k) return null;
    const i = k.lastIndexOf(".");
    const base = i >= 0 ? k.slice(0, i) : k;
    return `${base}.preview.png`;
  }
  function isObjectKey(s = "") {
    const x = String(s || "");
    return x.startsWith("storage/") || x.startsWith("catalog/");
  }
  function isHttpUrl(s = "") {
    return /^https?:\/\//i.test(String(s || ""));
  }
  // NEW: ตัดสินว่างานนี้ “ควรใช้ MinIO ก่อน” ไหม (Custom Storage / User History)
  function fromMinioOrHistory(job) {
    const src = (job?.source || "").toLowerCase();
    const gk  = job?.gcode_key || job?.gcode_path || "";
    return (
      isObjectKey(gk) ||
      src === "storage" ||
      src === "catalog" ||
      src === "history" ||
      src === "reprint" ||
      src === "user_history" ||
      src === "user-history"
    );
  }

  // อ่าน snapshot ที่ cache ไว้ใน localStorage (เพิ่มกุญแจ gcode/original)
  function getSnapshotFromCache(job) {
    try {
      // ตาม id (หลัง onConfirm)
      const idKeys = [job?.id, job?.job_id, job?.jobId].filter(Boolean);
      for (const id of idKeys) {
        const png = localStorage.getItem(`queueThumb:${id}`);
        if (png && png.startsWith("data:image/")) return png;
      }
      // ตาม gcode_key / original_key
      if (job?.gcode_key) {
        const byG = localStorage.getItem(`queueThumbByGcode:${job.gcode_key}`);
        if (byG && startsWithImage(byG)) return byG;
      }
      if (job?.original_key) {
        const byO = localStorage.getItem(`queueThumbByOrig:${job.original_key}`);
        if (byO && startsWithImage(byO)) return byO;
      }
      // ตามชื่อ (ก่อนรู้ id ทันทีหลังกด Confirm)
      if (job?.name) {
        const byName = localStorage.getItem(`queueThumbByName:${job.name}`);
        if (byName && startsWithImage(byName)) return byName;
      }
    } catch {}
    return null;
  }
  const startsWithImage = (s) => typeof s === "string" && s.startsWith("data:image/");

  // ดึงรูป preview สำหรับตาราง
  // ปรับลำดับชัดเจน:
  // (A) ถ้าเป็น Custom Storage / User History → ใช้ MinIO preview ก่อน, ถ้าไม่เจอค่อยถอยไป snapshot/อื่น ๆ
  // (B) งานทั่วไป → เดิม
  const pickPreview = (job) => {
    const cacheTag =
      job?.updated_at || job?.uploaded_at || job?.created_at || job?.started_at || job?.id || Date.now();

    if (fromMinioOrHistory(job)) {
      // 1) preview_key เป็น object key → /files/raw
      if (job?.preview_key && isObjectKey(job.preview_key) && /\.preview\.png$/i.test(job.preview_key)) {
        return toRawUrl(job.preview_key, cacheTag);
      }
      // 2) เดาจาก gcode_key/path → *.preview.png → /files/raw
      const gk = job?.gcode_key || job?.gcode_path || "";
      if (isObjectKey(gk)) {
        const pk = derivePreviewFromGcodeKey(gk);
        if (pk) return toRawUrl(pk, cacheTag);
      }
      // 3) thumb เป็น HTTP/presigned → ใช้ไปก่อน
      const t = job?.thumb || job?.thumbnail || job?.thumbnail_url || job?.previewUrl || "";
      if (isHttpUrl(t)) return t;

      // 4) ไม่พบใน MinIO → ค่อย fallback snapshot (ถ้ามี)
      const snap = getSnapshotFromCache(job);
      if (snap) return snap;

      // 5) ค่าอื่น ๆ/placeholder
      if (t && !isHttpUrl(t) && !isObjectKey(t)) return t;
      return placeholderImg;
    }

    // ----- งานทั่วไป (เดิม) -----
    const snap = getSnapshotFromCache(job);
    if (snap) return snap;

    const directUrl =
      job?.snapshotUrl ||
      job?.thumb_data_url ||
      job?.thumbnailUrl ||
      job?.preview_image_url ||
      job?.previewUrl;
    if (typeof directUrl === "string" && isHttpUrl(directUrl)) {
      return directUrl;
    }

    if (typeof directUrl === "string" && directUrl) {
      // BE ส่งเป็น “key”
      return toRawUrl(directUrl, cacheTag);
    }
    if (job?.preview_key) return toRawUrl(job.preview_key, cacheTag);
    if (job?.gcode_key) {
      const k = derivePreviewFromGcodeKey(job.gcode_key);
      if (k) return toRawUrl(k, cacheTag);
    }

    return job?.thumb || placeholderImg;
  };

  /* ===================== Drag & Drop Reorder (manager) ===================== */

  // ทำงานบนอาร์เรย์ในหน้า (optimistic) แล้วค่อยยิง API reorder
  const [localJobs, setLocalJobs] = useState(jobs);
  useEffect(() => setLocalJobs(jobs), [jobs]);

  // index ที่ลากอยู่
  const [dragIndex, setDragIndex] = useState(null);

  const isReorderable = (j) => j && (j.status === "queued" || j.status === "paused");
  const isProcessingRow = (idx) => idx === processingIndex && localJobs[idx]?.status === "processing";

  const printerIdForQueue = useMemo(() => {
    return (
      localJobs?.[0]?.printer_id ||
      localJobs?.[0]?.printerId ||
      localJobs?.[0]?.printer?.id ||
      process.env.REACT_APP_PRINTER_ID ||
      "prusa-core-one"
    );
  }, [localJobs]);

  // เรียก backend ส่งเฉพาะ job ids ที่สลับได้ (queued/paused)
  const callReorderApi = useCallback(async (orderedJobs) => {
    const ids = orderedJobs
      .filter(isReorderable)
      .map((j) => j.id)
      .filter((x) => x != null);

    if (ids.length === 0) return;

    try {
      if (api?.queue?.reorder) {
        await api.queue.reorder(printerIdForQueue, ids);
      } else {
        await api.post(`/printers/${encodeURIComponent(printerIdForQueue)}/queue/reorder`, { job_ids: ids });
      }
      // success → ไม่ต้องทำอะไรเพิ่ม รอ parent refresh/หรือ state local ใช้ต่อได้
    } catch (e) {
      // ถ้าพลาด → กลับไปลำดับเดิม
      setLocalJobs(jobs);
      try {
        window.dispatchEvent(new CustomEvent("toast", { detail: { type: "error", text: "Reorder failed" } }));
      } catch {}
    }
  }, [api, printerIdForQueue, jobs]);

  const onDragStart = (e, idx) => {
    if (!isReorderable(localJobs[idx])) return;
    setDragIndex(idx);
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", String(localJobs[idx].id)); } catch {}
  };

  const onDragOver = (e, idx) => {
    if (dragIndex == null) return;
    if (!isReorderable(localJobs[dragIndex])) return;
    // ห้าม drop ทับแถว Processing
    if (isProcessingRow(idx)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  };

  const onDrop = (e, idx) => {
    e.preventDefault();
    if (dragIndex == null) return;

    // ถ้าเป้าหมายเป็น processing → ไม่ทำ
    if (isProcessingRow(idx)) { setDragIndex(null); return; }

    // ห้ามย้าย "processing" เอง (แต่เราไม่ให้ลากตั้งแต่แรกแล้ว)
    if (!isReorderable(localJobs[dragIndex])) { setDragIndex(null); return; }

    // คัดลอกแล้วสลับ
    const next = localJobs.slice();
    const [moved] = next.splice(dragIndex, 1);

    // ถ้ามี processing อยู่หัวตาราง ให้กัน index 0 ไว้ (ห้าม drop ก่อนมัน)
    const headGuard = processingIndex === 0 ? 1 : 0;
    const safeIdx = Math.max(headGuard, Math.min(idx, next.length));
    next.splice(safeIdx, 0, moved);

    setLocalJobs(next);
    setDragIndex(null);

    // ยิง API ด้วยลำดับใหม่ (เฉพาะ queued/paused)
    callReorderApi(next);
  };

  const onDragEnd = () => setDragIndex(null);

  // ใช้ localJobs แสดงผล (optimistic)
  const viewJobs = localJobs;

  /* ===================== /Drag & Drop Reorder ============================== */

  return (
    <div className="printing-page">
      <table className="printing-table">
        <thead>
          <tr>
            <th>Part Name</th>
            <th>Status</th>
            <th>Waiting Time</th>
            <th></th>
          </tr>
        </thead>

        <tbody>
          {viewJobs.length === 0 && (
            <tr>
              <td colSpan={4} style={{ padding: "24px", color: "#667" }}>
                No print jobs yet. Use <strong>+ Print file</strong> or reprint from Storage/History.
              </td>
            </tr>
          )}

          {viewJobs.map((job, idx) => {
            const isProcessing = idx === processingIndex && job.status === "processing";
            const canCancel = canCancelJob(job, isProcessing);
            const cancelBtnClass = "btn-cancel" + (canCancel ? " red" : "");
            const isMine = (job?.uploadedBy === currentUserId || job?.employee_id === currentUserId);
            const isHighlight = highlightId != null && String(job.id) === String(highlightId);
            const imgSrc = pickPreview(job);

            // drag state / class
            const draggable = isReorderable(job); // queued/paused เท่านั้น
            const rowClass =
              `${isMine ? "row-own" : ""} ${isHighlight ? "just-queued" : ""} ` +
              `${draggable ? "can-drag" : ""} ${isProcessing ? "lock-proc" : ""}`;

            return (
              <tr
                key={job.id}
                className={rowClass}
                data-job-id={job.id}
                data-status={job.status}
                draggable={draggable}
                onDragStart={(e) => onDragStart(e, idx)}
                onDragOver={(e) => onDragOver(e, idx)}
                onDrop={(e) => onDrop(e, idx)}
                onDragEnd={onDragEnd}
                title={
                  isProcessing
                    ? "Processing jobs cannot be reordered. Pause or Cancel first."
                    : draggable
                      ? "Drag to reorder"
                      : "Finished jobs are not in the active queue"
                }
                style={draggable ? { cursor: "grab" } : undefined}
              >
                <td className="own-cell">
                  {isMine && <span className="own-bar" aria-hidden />}
                  <div className="part-cell">
                    <img
                      src={imgSrc}
                      alt="part"
                      className="part-img"
                      onError={onImgError}
                      style={{ width: 64, height: 64, borderRadius: 10, flex: "0 0 auto", objectFit: "cover" }}
                    />

                    <div className="part-meta">
                      {isMine && (
                        <div className="your-queue" id={`yq-${job.id}`}>Your Queue</div>
                      )}
                      <div className="part-id">{pad3(idx + 1)}</div>
                      <div className="part-name" title={job?.name || ""}>{stripExt(job?.name)}</div>
                      <div style={{ fontSize: 12, color: "#6b7280", fontWeight: 600, marginTop: 2 }}>
                        by {String(ownerOf(job))}
                      </div>
                    </div>
                  </div>
                </td>

                <td>
                  <span className={statusClassName(job.status)} aria-live="polite">
                    {statusText(job.status)}
                  </span>
                </td>

                <td className="waiting-time">
                  <strong>{waitingOrRemainingOf(idx)}</strong>
                </td>

                <td className="td-right">
                  <button
                    className={cancelBtnClass}
                    onClick={() => { if (canCancel) openConfirm(job); }}
                    disabled={!canCancel}
                    aria-disabled={!canCancel}
                    aria-label={canCancel ? "Cancel this job" : "You cannot cancel this job"}
                    title={
                      canCancel
                        ? "Cancel this job"
                        : isProcessing
                          ? "Cannot cancel while processing"
                          : ["completed","failed","canceled"].includes(job.status)
                            ? "This job has already finished"
                            : "You can cancel only your own job"
                    }
                  >
                    Cancel
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* FAB */}
      <button
        type="button"
        className="btn-print"
        onClick={() => setFabOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={fabOpen}
      >
        + Print file
      </button>

      {fabOpen && <div className="fab-scrim" onClick={() => setFabOpen(false)} />}
      {fabOpen && (
        <div className="fab-menu" role="menu" onClick={(e) => e.stopPropagation()}>
          <button type="button" className="fab-item" role="menuitem" onClick={openUploadSafely}>
            Upload File
          </button>

          <div className="fab-sep" aria-hidden />

          <button
            type="button"
            className="fab-item"
            role="menuitem"
            onClick={() => { setShowHistory(true); setFabOpen(false); }}
          >
            Print History
          </button>

          <div className="fab-sep" aria-hidden />

          <button type="button" className="fab-item" role="menuitem" onClick={goStorageSafely}>
            Custom Storage
          </button>
        </div>
      )}

      <UserPrintHistoryModal
        open={showHistory}
        onClose={() => setShowHistory(false)}
        // เด้งไฮไลต์ทันทีเมื่อได้ job กลับจาก API และส่งต่อให้ parent ถ้าต้องการ
        onPrinted={(job) => {
          const id = job?.id ?? job?.job_id ?? job?.jobId;
          if (id != null) {
            setHighlightId(String(id));
            setTimeout(() => setHighlightId(null), 6000);
          }
          onQueueFromHistory?.(job);
        }}
      />

      {/* ---------- Modal ยืนยันยกเลิก ---------- */}
      {confirmJob && (
        <div
          className="modal-scrim"
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-cancel-title"
          onClick={closeConfirm}
        >
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h3 id="confirm-cancel-title">Confirm to cancel this queue?</h3>
            <div className="modal-actions">
              <button type="button" className="btn-outline" onClick={closeConfirm} disabled={confirmBusy}>
                Go Back
              </button>
              <button
                type="button"
                className="btn-danger"
                onClick={handleConfirmCancel}
                disabled={confirmBusy}
              >
                {confirmBusy ? "Cancelling…" : "Yes, Cancel"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
