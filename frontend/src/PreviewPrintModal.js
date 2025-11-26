// src/PreviewPrintModal.js
import React, { useMemo, useEffect, useState, useRef, useCallback } from "react";
import { useApi } from "./api/index";
import { useAuth } from "./auth/AuthContext";
import GcodeWebGLPreview from "./GcodeWebGLPreview";

/* ===== regex สำหรับดึงข้อมูลจาก G-code (ทนทานหลายรูปแบบ) ===== */
const RE = {
  timeTxt: /;\s*estimated\s+printing\s+time(?:s)?(?:\s*\((?:normal|silent)\s*mode\))?\s*[:=]\s*([^\r\n]+)/i,
  timeSec: /;\s*TIME:\s*(\d+)/i,
  filG: /;\s*(?:total\s+filament\s+used|filament\s+used|used filament|estimated_filament_weight)\s*(?:\[\s*g\s*\]|\(g\)|g)?\s*[:=]?\s*([0-9.]+)\s*(?:g)?\b/i,
  filUsedCombo: /;\s*Used filament\s*:\s*([0-9.]+)\s*m\s*,\s*([0-9.]+)\s*g/i,
  filMm: /;\s*(?:filament(?:_used)?\s*\[mm\]|filament_mm)\s*[:=]?\s*([0-9.]+)/i,
  filVol:/;\s*(?:filament used\s*\[(?:mm3|mm\^3|cm3|cm\^3)]|filament_volume)\s*[:=]?\s*([0-9.]+)/i,
  density:/;\s*(?:filament(?:_density| density))(?:_g_cm3)?(?:\s*\[g\/cm3\])?\s*[:=]?\s*([0-9.]+)/i,
  diameter:/;\s*(?:filament(?:_diameter| diameter))(?:_mm)?(?:\s*\[mm\])?\s*([0-9.]+)/i,
  matType:/;\s*(?:filament_type|filament_settings_id)\s*=\s*([^\r\n]+)/i,
  firstEstTxt: /;\s*estimated\s+first\s+layer\s+printing\s+time(?:\s*\((?:normal|silent)\s*mode\))?\s*[:=]\s*([^\r\n]+)/i,
  firstTimeTxt: /;\s*first[_\s-]*layer(?:[_\s-]*(?:print|printing))?[_\s-]*time\s*=\s*([0-9hms:. ]+)/i,
  firstTimeSec: /;\s*first[_\s-]*layer(?:[_\s-]*(?:print|printing))?[_\s-]*time\s*:\s*([0-9]+)\s*(?:s|sec|seconds)?/i,
  layerMark: /^;\s*LAYER:\s*(-?\d+)/i,
  layerChange: /^;\s*LAYER_CHANGE\b/i,
  elapsed: /^;\s*TIME_ELAPSED:([0-9.]+)/i,
  // 👇 อ่าน support mode จาก header ของ PrusaSlicer
  supportMaterial: /;\s*support_material\s*=\s*([01])/i,
  supportBuildPlate: /;\s*support_material_buildplate_only\s*=\s*([01])/i,
};

const MATERIAL_DENSITY = {
  PLA:1.24, PETG:1.27, ABS:1.04, ASA:1.07, TPU:1.21, TPE:1.21,
  PC:1.20, PA:1.14, NYLON:1.14, HIPS:1.05, PET:1.34, PCTG:1.27
};
const MATERIAL_LABEL = {
  PLA:"PLA", PETG:"PETG", ABS:"ABS", ASA:"ASA", TPU:"TPU", TPE:"TPE",
  PC:"PC", PA:"Nylon", NYLON:"Nylon", HIPS:"HIPS", PET:"PET", PCTG:"PCTG"
};

// mapping โหมด support → label ที่โชว์ใน UI
const SUPPORT_LABEL = {
  none: "None",
  build_plate_only: "Support on build plate only",
  enforcers_only: "For support enforcers only",
  everywhere: "Everywhere",
};

const DEFAULT_DIAMETER = 1.75;
const DEFAULT_DENSITY = 1.27;

const formatSeconds = (sec) => {
  const s = Math.max(0, (sec | 0) * 1);
  const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0, ss = s % 60;
  return h ? `${h}h ${m}m ${ss}s` : m ? `${m}m ${ss}s` : `${ss}s`;
};

function normalizeMaterialKey(x = "") {
  const up = String(x).trim().toUpperCase();
  if (up.includes("PCTG")) return "PCTG";
  if (up.includes("PETG")) return "PETG";
  if (/\bPLA\b/.test(up)) return "PLA";
  if (/\bABS\b/.test(up)) return "ABS";
  if (/\bASA\b/.test(up)) return "ASA";
  if (/(TPU|FLEX)/.test(up)) return "TPU";
  if (/(PA|NYLON)/.test(up)) return "PA";
  if (/\bPC\b/.test(up)) return "PC";
  if (/\bHIPS\b/.test(up)) return "HIPS";
  if (/\bPET\b/.test(up)) return "PET";
  return null;
}

function parseFirstLayerTime(txt) {
  if (!txt) return "";
  const est = RE.firstEstTxt.exec(txt); if (est) return est[1].trim();
  const a = RE.firstTimeTxt.exec(txt);  if (a)   return a[1].trim();
  const b = RE.firstTimeSec.exec(txt);  if (b)   return formatSeconds(parseInt(b[1], 10));

  let sawL0 = false, passedL0 = false, lastElapsed = null, t0 = null, t1 = null;
  for (const line of txt.split(/\r?\n/)) {
    const mE = RE.elapsed.exec(line);
    if (mE) {
      lastElapsed = parseFloat(mE[1]);
      if (sawL0 && !passedL0 && t0 == null) t0 = lastElapsed;
      if (passedL0 && t1 == null) { t1 = lastElapsed; break; }
      continue;
    }
    const mL = RE.layerMark.exec(line);
    if (mL) {
      const n = parseInt(mL[1], 10);
      if (n === 0 && !sawL0) { sawL0 = true; if (lastElapsed != null) t0 = lastElapsed; }
      else if (sawL0 && n >= 1 && !passedL0) { passedL0 = true; if (lastElapsed != null) { t1 = lastElapsed; break; } }
      continue;
    }
    if (RE.layerChange.test(line)) {
      if (!sawL0) { sawL0 = true; if (lastElapsed != null) t0 = lastElapsed; }
      else if (!passedL0) { passedL0 = true; if (lastElapsed != null) t1 = lastElapsed; break; }
    }
  }
  if (t0 != null && t1 != null && t1 > t0) return formatSeconds(Math.round(t1 - t0));
  return "";
}

function parseInfoFromGcode(txt) {
  if (!txt) return {};
  const info = {};

  const t1 = RE.timeTxt.exec(txt); if (t1) info.total_text = t1[1].trim();
  const t2 = RE.timeSec.exec(txt); if (t2 && !info.total_text) info.total_text = formatSeconds(parseInt(t2[1], 10));

  const matMatch = RE.matType.exec(txt);
  if (matMatch) {
    const key = normalizeMaterialKey(matMatch[1]);
    if (key) info.material = key;
  }

  const combo = RE.filUsedCombo.exec(txt);
  if (combo) {
    const g = parseFloat(combo[2]);
    if (Number.isFinite(g)) info.filament_g = +g.toFixed(2);
  } else {
    const fg = RE.filG.exec(txt);
    if (fg) {
      const n = parseFloat(fg[1]);
      if (Number.isFinite(n)) info.filament_g = +n.toFixed(2);
    } else {
      const fmm = RE.filMm.exec(txt)?.[1];
      const fvol = RE.filVol.exec(txt)?.[1];
      let dens = RE.density.exec(txt)?.[1];
      let diam = RE.diameter.exec(txt)?.[1];

      if (!dens && matMatch) {
        const key = normalizeMaterialKey(matMatch[1]);
        if (key && MATERIAL_DENSITY[key]) dens = MATERIAL_DENSITY[key];
      }
      if (!dens) dens = DEFAULT_DENSITY;
      if (!diam) diam = DEFAULT_DIAMETER;

      const mmVal = fmm ? parseFloat(fmm) : null;
      const mm3Val = fvol ? parseFloat(fvol) : null;
      const density = Number(dens), diameter = Number(diam);

      let volMm3 = mm3Val;
      if (!volMm3 && Number.isFinite(mmVal) && Number.isFinite(diameter)) {
        const r = diameter / 2; volMm3 = mmVal * Math.PI * r * r;
      }
      if (Number.isFinite(volMm3) && Number.isFinite(density)) {
        info.filament_g = +((volMm3 / 1000) * density).toFixed(2);
      } else {
        if (Number.isFinite(mmVal))  info.filament_mm  = +mmVal.toFixed(1);
        if (Number.isFinite(mm3Val)) info.filament_mm3 = +mm3Val.toFixed(1);
      }
    }
  }

  const fl = parseFirstLayerTime(txt);
  if (fl) info.first_layer = fl;

  // 👇 support mode จาก header ของ PrusaSlicer
  const sm = RE.supportMaterial.exec(txt);
  if (sm) {
    const on = sm[1].trim() === "1";
    if (!on) {
      info.support = "none";
    } else {
      const bp = RE.supportBuildPlate.exec(txt);
      const bpOnly = bp && bp[1].trim() === "1";
      info.support = bpOnly ? "build_plate_only" : "everywhere";
    }
  }

  return info;
}

function mergeInfo(prev = {}, next = {}) {
  const out = { ...prev };
  for (const k of Object.keys(next)) {
    const v = next[k];
    if (v == null || v === "") continue;
    if (k === "total_text" || k === "filament_g" || k === "material" || k === "support") out[k] = v;
    else if (out[k] == null) out[k] = v;
  }
  return out;
}

/* ===================================================================== */

export default function PreviewPrintModal({
  open,
  onClose,
  data,
  onConfirm,
  confirming = false
}) {
  const api = useApi();
  const { token } = useAuth();

  // ใช้ ref เพื่อไม่ให้ useCallback เปลี่ยนทุกครั้งที่ token/api เปลี่ยน
  const apiRef = useRef(api);
  useEffect(() => { apiRef.current = api; }, [api]);

  const tokenRef = useRef(token);
  useEffect(() => { tokenRef.current = token; }, [token]);

  const gcodeKey    = data?.gcodeId ?? data?.gcodeKey ?? data?.gcode_key ?? null;
  const originalKey = data?.originalFileId ?? data?.originalKey ?? data?.original_key ?? null;
  const snapshotUrl = data?.snapshotUrl || data?.preview_image_url || null;

  const [gcodeInfo, setGcodeInfo] = useState(null);
  const [loadingInfo, setLoadingInfo] = useState(false);
  const [errInfo, setErrInfo] = useState("");
  const [confirmErr, setConfirmErr] = useState("");
  const runSeq = useRef(0);

  // ----- low-level helpers -----
  const textFromResp = async (resp) => {
    const ab = await resp.arrayBuffer();
    return new TextDecoder().decode(new Uint8Array(ab));
  };

  // 1) /api/storage/range (head)
  const fetchGcodeChunkViaApi = useCallback(
    async (start, length = 4_000_000) => {
      if (!gcodeKey) throw new Error("no gcode key");
      if (start < 0) throw new Error("negative-range-not-supported-by-api");
      const base = apiRef.current.API_BASE || "";
      const u = new URL(base + "/api/storage/range", window.location.origin);
      u.searchParams.set("object_key", gcodeKey);
      u.searchParams.set("start", String(start));
      u.searchParams.set("length", String(length));
      const t = tokenRef.current;
      const headers = t ? { Authorization: `Bearer ${t}` } : {};
      const resp = await fetch(u.toString(), { headers });
      if (!(resp.ok || resp.status === 206)) throw new Error(`/api/storage/range ${resp.status}`);
      return textFromResp(resp);
    },
    [gcodeKey]
  );

  // 2) presign (รู้ขนาดไฟล์)
  const fetchGcodeChunkViaPresign = useCallback(
    async (start, length = 4_000_000) => {
      if (!gcodeKey) throw new Error("no gcode key");
      const pres = await apiRef.current.storage
        .presignGet(gcodeKey, /*withMeta*/ true)
        .catch(() => null);

      const url = pres?.url;
      const size = Number(pres?.size ?? 0);
      if (!url) throw new Error("presignGet failed");
      if (!Number.isFinite(size) || size <= 0) throw new Error("presign-without-size");

      const len = Math.max(0, length);
      let from = 0, to = 0;
      if (start >= 0) {
        if (start >= size) return "";
        from = start;
      } else {
        from = Math.max(0, size - len);
      }
      to = Math.min(size - 1, from + len - 1);

      const resp = await fetch(url, { headers: { Range: `bytes=${from}-${to}` } });
      if (!(resp.ok || resp.status === 206)) {
        if (resp.status === 416) throw new Error("presign-416");
        throw new Error(`presign range ${resp.status}`);
      }
      return textFromResp(resp);
    },
    [gcodeKey]
  );

  // 3) /files/raw (fallback)
  const fetchGcodeChunkViaFilesRaw = useCallback(
    async (start, length = 4_000_000) => {
      if (!gcodeKey) throw new Error("no gcode key");
      const base = apiRef.current.API_BASE || "";
      const u = new URL(base + "/files/raw", window.location.origin);
      u.searchParams.set("object_key", gcodeKey);
      const t = tokenRef.current;
      const headers = t ? { Authorization: `Bearer ${t}` } : {};
      const resp = await fetch(u.toString(), { headers });
      if (!resp.ok) throw new Error(`/files/raw ${resp.status}`);
      const full = await resp.text();
      if (start >= 0) return full.slice(start, start + length);
      const sliceLen = Math.max(0, length);
      return full.slice(Math.max(0, full.length - sliceLen));
    },
    [gcodeKey]
  );

  // orchestrator
  const fetchGcodeChunk = useCallback(
    async (start, length = 4_000_000) => {
      if (start >= 0) {
        try { return await fetchGcodeChunkViaApi(start, length); } catch {}
      }
      try { return await fetchGcodeChunkViaPresign(start, length); }
      catch { return await fetchGcodeChunkViaFilesRaw(start, length); }
    },
    [fetchGcodeChunkViaApi, fetchGcodeChunkViaPresign, fetchGcodeChunkViaFilesRaw]
  );

  // ----- read head/tail for info -----
  useEffect(() => {
    let alive = true;
    const seq = ++runSeq.current;
    (async () => {
      setErrInfo("");
      if (!open || !gcodeKey) return;
      setLoadingInfo(true);
      try {
        let parsed = {};
        try {
          const headTxt = await fetchGcodeChunk(0, 4_000_000);
          if (headTxt) parsed = parseInfoFromGcode(headTxt);
        } catch {}
        try {
          const tailTxt = await fetchGcodeChunk(-4_000_000, 4_000_000);
          if (tailTxt) parsed = mergeInfo(parsed, parseInfoFromGcode(tailTxt));
        } catch {}
        if (Object.keys(parsed).length && alive && seq === runSeq.current) {
          setGcodeInfo((p) => mergeInfo(p || {}, parsed));
        }
      } catch (e) {
        if (alive && seq === runSeq.current) setErrInfo(String(e?.message || "Failed to fetch G-code info"));
      } finally {
        if (alive && seq === runSeq.current) setLoadingInfo(false);
      }
    })();
    return () => { alive = false; };
  }, [open, gcodeKey, fetchGcodeChunk]);

  const pick = (...vals) => vals.find((v) => v != null && v !== "");
  const fileBase = (k) => String(k || "").split("/").pop();
  const displayName =
    pick(
      data?.settings?.name,
      data?.fileName,
      data?.originalName,
      fileBase(originalKey || ""),
      fileBase((data?.gcodeKey || data?.gcode_key) || "")
    ) || "Unnamed";

  const materialKey =
    pick(
      data?.settings?.material && normalizeMaterialKey(data.settings.material),
      data?.material && normalizeMaterialKey(data.material),
      gcodeInfo?.material
    );
  const materialDisplay = materialKey ? (MATERIAL_LABEL[materialKey] || materialKey) : "-";

  // 👇 ใช้ค่าจาก G-code เป็นหลัก ถ้าไม่มีค่อย fallback ไป settings
  const supportModeForDisplay =
    gcodeInfo?.support ||
    data?.settings?.support ||
    null;

  const supportDisplay =
    supportModeForDisplay
      ? (SUPPORT_LABEL[supportModeForDisplay] || supportModeForDisplay)
      : (originalKey ? "-" : "—");

  const filamentDisplay = useMemo(() => {
    if (Number.isFinite(gcodeInfo?.filament_g)) return `${Number(gcodeInfo.filament_g).toFixed(2)} g`;
    if (Number.isFinite(gcodeInfo?.filament_mm))  return `${gcodeInfo.filament_mm} mm`;
    if (Number.isFinite(gcodeInfo?.filament_mm3)) return `${gcodeInfo.filament_mm3} mm³`;
    return "-";
  }, [gcodeInfo]);

  const parseMin = (txt) => {
    if (!txt || txt === "-") return null;
    const s = String(txt).trim();
    if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(s)) {
      const parts = s.split(":").map((t) => parseInt(t, 10));
      let h=0,m=0,sec=0; if (parts.length===3) [h,m,sec]=parts; else if (parts.length===2) [m,sec]=parts;
      const minutes = Math.round((h*3600+m*60+sec)/60);
      return minutes > 0 ? minutes : null;
    }
    const H=/(\d+)\s*h/i.exec(s), M=/(\d+)\s*m/i.exec(s), S=/(\d+)\s*s/i.exec(s);
    const sec=(H?+H[1]*3600:0)+(M?+M[1]*60:0)+(S?+S[1]:0);
    if (sec) return Math.round(sec/60);
    return null;
  };

  const timeTotalText   = gcodeInfo?.total_text || "-";
  const timeMinForPost  = useMemo(() => parseMin(gcodeInfo?.total_text) ?? 0, [gcodeInfo?.total_text]);

  const hasLocalGcodePath =
    typeof (data?.gcodeUrl ?? data?.gcode_url ?? "") === "string" &&
    (data?.gcodeUrl ?? data?.gcode_url ?? "").startsWith("/uploads/");
  const canConfirm = Boolean(gcodeKey || hasLocalGcodePath);

  // ---------- capture preview (WebGL/DOM) ----------
  const previewWrapRef = useRef(null);
  const viewerRef = useRef(null);

  // ตรวจว่าภาพจาก canvas ว่าง/โปร่งใส/ขาวล้วนหรือไม่
  function isBlankImage(canvas) {
    try {
      const w = Math.max(1, canvas?.width  || canvas?.clientWidth  || 520);
      const h = Math.max(1, canvas?.height || canvas?.clientHeight || 360);
      const t = document.createElement("canvas");
      t.width = w; t.height = h;
      const ctx = t.getContext("2d");
      ctx.drawImage(canvas, 0, 0, w, h);

      const pts = [
        [0,0],[w>>1,0],[w-1,0],
        [0,h>>1],[w>>1,h>>1],[w-1,h>>1],
        [0,h-1],[w>>1,h-1],[w-1,h-1],
      ];
      let nonEmpty = 0;
      for (const [x,y] of pts) {
        const d = ctx.getImageData(Math.max(0,x-1), Math.max(0,y-1), 3, 3).data;
        for (let i=0; i<d.length; i+=4) {
          const a = d[i+3], r=d[i], g=d[i+1], b=d[i+2];
          const bright = (r+g+b)/3;
          if (a>8 && bright<250) { nonEmpty++; break; }
        }
        if (nonEmpty) break;
      }
      return nonEmpty === 0;
    } catch { return false; }
  }

  // รอ canvas วาดจริง + รีทรายหลายครั้ง จนได้ dataURL ที่ไม่ว่าง
  async function grabCanvasDataURL() {
    // 1) เมธอดจาก component (ถ้ามี)
    try {
      const viaRef = viewerRef.current?.getSnapshot?.();
      if (viaRef && viaRef.startsWith?.("data:image/")) return viaRef;
      if (viewerRef.current?.snapshotAsync) {
        const s = await viewerRef.current.snapshotAsync();
        if (s && s.startsWith?.("data:image/")) return s;
      }
    } catch {}

    // 2) ลูปรอ frame + backoff (สูงสุด 12 ครั้ง)
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      try {
        const wrap = previewWrapRef.current;
        const canvas = wrap?.querySelector?.("canvas");
        if (canvas && typeof canvas.toDataURL === "function") {
          if (!isBlankImage(canvas)) {
            const data = canvas.toDataURL("image/png", 0.92);
            if (data && data.startsWith("data:image/")) return data;
          }
        }
        // fallback: <img> snapshot
        const img = wrap?.querySelector?.("img");
        if (img?.src) {
          if (img.src.startsWith("data:image/")) return img.src;
          const c = document.createElement("canvas");
          c.width = img.naturalWidth || img.width || 520;
          c.height = img.naturalHeight || img.height || 360;
          const ctx = c.getContext("2d");
          ctx.drawImage(img, 0, 0, c.width, c.height);
          if (!isBlankImage(c)) {
            const data = c.toDataURL("image/png", 0.92);
            if (data && data.startsWith("data:image/")) return data;
          }
        }
      } catch {}
      await new Promise(r => setTimeout(r, 80 + attempt * 40));
    }
    return null;
  }

  const handleConfirm = async () => {
    setConfirmErr("");
    if (!canConfirm || confirming) return;

    const thumbPng = await grabCanvasDataURL();

    const payload = {
      name: displayName,
      time_min: timeMinForPost || 0,
      time_text: gcodeInfo?.total_text || null,
      filament_g: Number.isFinite(gcodeInfo?.filament_g) ? Number(gcodeInfo.filament_g) : null,
      source: "upload",
      // รองรับทั้ง BE เก่า/ใหม่
      thumb: thumbPng || undefined,
      thumb_data_url: thumbPng || undefined,
    };
    if (gcodeKey) payload.gcode_key = gcodeKey;
    else if (hasLocalGcodePath) payload.gcode_path = (data?.gcodeUrl ?? data?.gcode_url ?? "");
    if (originalKey) payload.original_key = originalKey;

    // cache ชั่วคราวก่อนรู้ id — เก็บให้ครบทั้ง name/gcode/original
    try {
      if (thumbPng) {
        if (displayName)  localStorage.setItem(`queueThumbByName:${displayName}`, thumbPng);
        if (gcodeKey)     localStorage.setItem(`queueThumbByGcode:${gcodeKey}`, thumbPng);
        if (originalKey)  localStorage.setItem(`queueThumbByOrig:${originalKey}`, thumbPng);
        // กระตุ้นหน้า Printing ให้รีเฟรชรูปทันที
        window.dispatchEvent(new CustomEvent("queue-thumb", { detail: { name: displayName, gcodeKey, originalKey } }));
      }
    } catch {}

    try {
      const res = await onConfirm?.(payload);

      // เมื่อได้ id → เก็บตาม id แล้วลบ key แบบชื่อ
      if (thumbPng && res) {
        const ids = [
          res?.id, res?.job_id, res?.jobId,
          res?.data?.id, res?.data?.job_id, res?.data?.jobId,
          res?.queue_id, res?.queueItemId, res?.queue_item_id
        ].filter(Boolean);
        for (const k of ids) {
          try { localStorage.setItem(`queueThumb:${k}`, thumbPng); } catch {}
        }
        if (ids.length && displayName) {
          try { localStorage.removeItem(`queueThumbByName:${displayName}`); } catch {}
        }
        try {
          window.dispatchEvent(new CustomEvent("queue-thumb", { detail: { id: ids[0], name: displayName, gcodeKey, originalKey } }));
        } catch {}
      }

      onClose?.();
    } catch (e) {
      setConfirmErr(String(e?.message || "Failed to queue the print"));
    }
  };

  const onScrimClick = () => { if (!confirming) onClose?.(); };

  if (!open || !data) {
    return null;
  }

  return (
    <div className="pv-scrim" onClick={onScrimClick} role="presentation">
      <div
        className="pv-card"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="pv-title"
      >
        <button type="button" className="pv-close" onClick={onScrimClick} aria-label="Close" disabled={confirming}>×</button>

        <div className="pv-head">
          <div id="pv-title" className="pv-title">Preview</div>
          <div className="pv-head-sep" aria-hidden />
          <div className="pv-file" title={displayName}>{displayName}</div>
        </div>

        <div className="pv-body">
          <div
            className="pv-canvas"
            style={{
              minWidth: 520,
              position: "relative",
              aspectRatio: "16 / 10",
              minHeight: 320,
              borderRadius: 12,
              background: "#f6f7f9",
              overflow: "hidden"
            }}
            ref={previewWrapRef}
          >
            {gcodeKey ? (
              <GcodeWebGLPreview
                ref={viewerRef}
                objectKey={gcodeKey}
                token={token}
                apiBase={apiRef.current.API_BASE}
                preset="clean"
                last={3}
                fitTarget="model"
                fitFactor={1.02}
                gridPadMM={6}
                minGridSize={110}
                preserveDrawingBuffer
                style={{ width: "100%", height: "100%" }}
              />
            ) : snapshotUrl ? (
              <img
                alt="G-code preview (snapshot)"
                src={snapshotUrl}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            ) : (
              <div style={{ width:"100%", height:"100%", display:"grid", placeItems:"center", color:"#9aa0a6" }}>
                No G-code provided.
              </div>
            )}
          </div>

          <div className="pv-side">
            <section className="pv-block">
              <h3 className="pv-block-title">Print Setting</h3>
              <dl className="pv-dl">
                <dt className="pv-linkish">Printer</dt><dd>{data?.printer || "PrusaSlicer"}</dd>
                <dt className="pv-linkish">Material</dt><dd>{materialDisplay}</dd>
                <dt className="pv-linkish">Model</dt><dd>{data?.settings?.model ?? "-"}</dd>
                <dt className="pv-linkish">Sparse infill density</dt><dd>{data?.settings?.infill ?? (originalKey ? "-" : "—")}</dd>
                <dt className="pv-linkish">Wall loops</dt><dd>{data?.settings?.walls ?? (originalKey ? "-" : "—")}</dd>
                <dt className="pv-linkish">Support</dt>
                <dd>{supportDisplay}</dd>
              </dl>
            </section>

            <section className="pv-block">
              <h3 className="pv-block-title pv-with-rule">Slicing Result</h3>
              <dl className="pv-dl">
                <dt className="pv-linkish">Used Filament</dt><dd>{filamentDisplay}</dd>
                <dt className="pv-linkish">First layer</dt><dd>{gcodeInfo?.first_layer ?? "-"}</dd>
                <dt className="pv-linkish">Time Total</dt><dd>{timeTotalText}</dd>
                {loadingInfo && <dd style={{ gridColumn: "1 / -1", color: "#9aa0a6" }}>Reading G-code info…</dd>}
                {errInfo &&     <dd style={{ gridColumn: "1 / -1", color: "#b74d4d" }}>{errInfo}</dd>}
              </dl>
            </section>

            {confirmErr && (
              <div style={{ color:"#b74d4d", fontSize:13, marginTop:6 }}>
                {confirmErr}
              </div>
            )}
          </div>
        </div>

        <button
          type="button"
          className="pv-cta preview-confirm"
          onClick={handleConfirm}
          disabled={!canConfirm || confirming}
          aria-disabled={!canConfirm}
          aria-busy={confirming ? "true" : "false"}
        >
          {confirming ? "Processing…" : "Confirm Print"}
        </button>
      </div>
    </div>
  );
}