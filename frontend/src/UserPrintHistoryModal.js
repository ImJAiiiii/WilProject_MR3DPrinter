// UserPrintHistoryModal.js
import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import "./UserPrintHistoryModal.css";
import { useAuth } from "./auth/AuthContext";
import { useApi } from "./api";
import { getUserHistory } from "./history";

const LS_HISTORY = "userHistory";

/* ---------------- local utils ---------------- */
const readJSON = (k, d) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } };
const writeJSON = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };
const jsonEqual = (a, b) => { try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; } };

const stripHashPrefix = (s) => String(s || "").replace(/^[a-f0-9]{8,32}[_-]/i, "");
const stripGcodeExt  = (s) => String(s || "").replace(/\.(gcode|gco|gc)$/i, "");

// "3h 12m" -> 192
function timeTextToMin(text) {
  if (!text) return null;
  const h_m = /(\d+)\s*h\s*(\d+)\s*m/i.exec(text);
  if (h_m) return (+h_m[1]) * 60 + (+h_m[2]);
  const m = /(\d+)\s*m/i.exec(text);
  if (m) return +m[1];
  return null;
}
function fmtMinutes(min) {
  if (min == null) return "-";
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

const SUPPORT_LABEL = {
  none: "None",
  build_plate_only: "Support on build plate only",
  enforcers_only: "For support enforcers only",
  everywhere: "Everywhere",
};

// เหมือนใน ModalUpload
const normalizePrinterId = (x) => {
  const s = String(x || "").trim();
  const def = process.env.REACT_APP_PRINTER_ID || "prusa-core-one";
  if (!s) return def;
  const slug = s
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/--+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (slug.startsWith("prusa") && slug.includes("core") && slug.includes("one"))
    return "prusa-core-one";
  return slug || def;
};

// รวม/เดาดอกกุญแจ gcode
function pickGcodeKeyLike(obj) {
  return (
    obj?.gcode_key ||
    obj?.object_key ||
    obj?.file?.object_key ||
    obj?.gcode?.key ||
    obj?.storage?.key ||
    obj?.manifest?.gcode_key ||
    null
  );
}

/* ---------- helpers สำหรับ manifest / รูป preview ---------- */
const toNum = (v) => {
  if (v === "" || v == null) return null;
  const n = Number(String(v).replace(/[, ]+/g, ""));
  return Number.isFinite(n) ? n : null;
};
const fromPercent = (s) => {
  if (s == null) return null;
  const m = String(s).match(/(-?[\d.]+)/);
  const n = m ? Number(m[1]) : NaN;
  if (!Number.isFinite(n)) return null;
  if (n > 0 && n <= 1) return Math.round(n * 100);
  return Math.max(0, Math.min(100, Math.round(n)));
};
const cleanString = (s) => {
  if (s == null) return null;
  let t = String(s).trim();
  if (t === '""' || t === "''" || t === '"') t = "";
  t = t.split("\n")[0];
  if (t.includes(";")) t = t.split(";")[0];
  if (t.includes("=")) t = t.split("=")[0];
  t = t.trim();
  return t || null;
};
const asBool = (v) => {
  if (typeof v === "boolean") return v;
  const t = String(v ?? "").trim().toLowerCase();
  if (["1","true","yes","y"].includes(t)) return true;
  if (["0","false","no","n","none"].includes(t)) return false;
  return null;
};
const deriveModelFromKey = (key) => {
  try {
    if (!key) return null;
    const parts = String(key).split("/");
    return parts[0] === "catalog" && parts[1] ? parts[1] : null;
  } catch { return null; }
};
const guessNozzle = (s) => {
  if (!s) return null;
  const m = String(s).match(/(\d+(?:\.\d+)?)\s*mm/i) || String(s).match(/(\d\.\d)/);
  return m ? Number(m[1]) : null;
};
const materialShort = (s) => {
  const t = cleanString(s);
  if (!t) return null;
  const m = t.match(/\b(PLA|PETG|ABS|ASA|TPU)\b/i);
  return m ? m[1].toUpperCase() : t;
};
/** เดา candidate keys ของ manifest จาก gcode_key / ไฟล์แนบ */
function guessManifestKeys(gcodeKey, jsonKeyFromFile) {
  const out = new Set();
  if (jsonKeyFromFile) out.add(jsonKeyFromFile);
  const k = String(gcodeKey || "");
  if (!k) return Array.from(out);
  out.add(k.replace(/\.(gcode|gco|gc)$/i, ".json"));
  out.add(k.replace(/\.(gcode|gco|gc)$/i, ".manifest.json"));
  out.add(k.replace(/\.(gcode|gco|gc)$/i, ".meta.json"));
  out.add(k.replace(/\.(gcode|gco|gc)$/i, "-meta.json"));
  return Array.from(out);
}
const mapManifest = (man = {}) => {
  const summary = man.summary || man.stats || {};
  const applied = man.applied || man.settings?.applied || man.settings || {};
  const presets = (man.slicer && man.slicer.presets) || {};

  const printer =
    cleanString(presets.printer_profile || presets.printer ||
                applied.printer_profile || applied.printer);

  const material =
    materialShort((man.slicer && man.slicer.material) ||
                  presets.filament || applied.material);

  const wallLoops =
    toNum(applied.wall_loops) ??
    toNum(applied.wallLoops) ??
    toNum(applied.perimeters) ??
    toNum(applied.perimeter_loops) ?? null;

  const supportsBool =
    asBool(applied.support) ??
    asBool(applied.supports) ??
    (applied.support_material != null ? !!applied.support_material : null);

  let support_mode = null;
  const sv = String(applied.support_mode ?? applied.support ?? applied.supports ?? "")
               .trim().toLowerCase();
  if (sv) {
    if (["none","no","0","false"].includes(sv)) support_mode = "none";
    else if (sv.includes("build") || sv.includes("plate")) support_mode = "build_plate_only";
    else if (sv.includes("enforcer")) support_mode = "enforcers_only";
    else if (sv.includes("everywhere") || sv.includes("all")) support_mode = "everywhere";
  }

  const template = {
    profile:  cleanString(presets.print || applied.profile),
    printer,
    material,
    layer:    toNum(applied.first_layer_height ?? applied.layer_height),
    nozzle:   toNum(applied.nozzle) ?? guessNozzle(presets.printer),
    infill:   fromPercent(applied.fill_density ?? applied.infill ?? applied.infill_density),
    supports: supportsBool,
    support_mode: support_mode || undefined,
    wallLoops,
    model:    deriveModelFromKey(man.gcode_key) || man.model || null,
  };

  const minutes =
    toNum(summary.estimate_min ?? summary.estimateMin ?? summary.time_min) ??
    (toNum(summary.seconds) ? Math.round(Number(summary.seconds)/60) : null);

  const timeText =
    cleanString(summary.total_text || summary.time_text) ||
    (Number.isFinite(minutes) ? fmtMinutes(minutes) : null);

  const grams =
    toNum(summary.filament_g ?? summary.filamentG ?? summary.filament_grams ?? summary.filament_g_total);

  const stats = { minutes, timeText, grams };
  const keys = {
    preview_key: man.preview_key || man.meta?.preview_key || null,
    gcode_key:   man.gcode_key   || man.meta?.gcode_key   || null,
  };
  return { template, stats, keys };
};

// แนบ token ในพารามฯ เพื่อให้ <img> โหลดผ่าน /files/raw ได้
const toRawUrl = (base, key, token) => {
  if (!key) return null;
  const u = new URL(`${base}/files/raw`);
  u.searchParams.set("object_key", key);
  if (token) u.searchParams.set("token", token);
  return u.toString();
};
const resolveThumbSrc = (apiBase, v, token) => {
  const s = String(v || "");
  if (!s) return "";
  if (s.startsWith("data:")) return s;
  if (s.startsWith("http://") || s.startsWith("https://")) return s;
  if (s.startsWith("storage/") || s.startsWith("catalog/")) return toRawUrl(apiBase, s, token);
  return s;
};
const derivePreviewKey = (key) => (key ? key.replace(/\.(gcode|gco|gc)$/i, ".preview.png") : null);

/* ---------- grams fallback จากท้าย G-code ---------- */
const gramsFromGcodeTail = (text) => {
  if (!text) return null;
  let m = text.match(/^\s*;\s*filament\s+used\s*\[g\]\s*=\s*([\d.]+)/im);
  if (m) return Number(m[1]);
  m = text.match(/^\s*;\s*filament\s*used\s*:\s*([\d.]+)\s*g/i) || text.match(/^\s*;\s*FILAMENT_USED:([\d.]+)g/i);
  if (m) return Number(m[1]);
  const mm = (() => {
    const a = text.match(/^\s*;\s*filament\s+used\s*\[mm\]\s*=\s*([\d.]+)/im);
    const b = text.match(/^\s*;\s*Filament\s*length\s*:\s*([\d.]+)\s*mm/i);
    return a ? Number(a[1]) : (b ? Number(b[1]) : null);
  })();
  if (mm) {
    const dia = (() => {
      const d = text.match(/^\s*;\s*filament_diameter\s*=\s*([\d.]+)/im) ||
                text.match(/^\s*;\s*Filament\s*Diameter\s*:\s*([\d.]+)\s*mm/i);
      return d ? Number(d[1]) : 1.75;
    })();
    const density = (() => {
      const d = text.match(/^\s*;\s*filament_density\s*=\s*([\d.]+)/im) ||
                text.match(/^\s*;\s*Material\s*Density\s*:\s*([\d.]+)/i);
      return d ? Number(d[1]) : 1.24;
    })();
    const r = dia / 2;
    const area = Math.PI * r * r;
    const vol_mm3 = area * mm;
    const vol_cm3 = vol_mm3 / 1000;
    return Math.round(vol_cm3 * density * 100) / 100;
  }
  return null;
};
async function fetchGcodeTail(apiBase, key, token) {
  if (!key) return null;
  const url = `${apiBase}/files/raw?object_key=${encodeURIComponent(key)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  try {
    const res = await fetch(url, { headers: { Range: "bytes=-65536" } });
    if (!res.ok && res.status !== 206) {
      const full = await fetch(url);
      if (!full.ok) return null;
      const text = await full.text();
      return text.slice(-65536);
    }
    return await res.text();
  } catch {
    return null;
  }
}

/* ---------- adapters ---------- */

// สร้างชื่ออ่านง่าย (ตัด hash + .gcode)
const prettyName = (raw) => stripGcodeExt(stripHashPrefix(raw || "Unnamed"));

function fromServerHistoryItem(j) {
  const uploadedAt =
    j.finished_at ? new Date(j.finished_at).getTime()
    : j.uploaded_at ? new Date(j.uploaded_at).getTime()
    : Date.now();

  const gcodeKey = pickGcodeKeyLike(j);

  const template = (() => {
    const t = j?.template && typeof j.template === "object" ? { ...j.template } : null;
    const settings =
      (j?.settings && typeof j.settings === "object" ? j.settings : null) ||
      (t && typeof t.settings === "object" ? t.settings : null);
    if (!t && !settings) return null;
    const merged = { ...(t || {}) };
    if (settings) {
      for (const [k, v] of Object.entries(settings)) {
        if (merged[k] == null || merged[k] === "" || merged[k] === 0 || merged[k] === false) {
          merged[k] = v;
        }
      }
    }
    if (merged.settings && typeof merged.settings === "object") delete merged.settings;
    return merged;
  })();

  const summary = j?.manifest?.summary || {};
  const time_min =
    j.time_min ??
    j?.stats?.time_min ??
    (Number.isFinite(summary.estimate_min) ? Math.round(summary.estimate_min) : null);
  const time_text =
    j?.stats?.time_text ??
    summary.total_text ??
    (Number.isFinite(time_min) ? fmtMinutes(time_min) : null);
  const filament_g =
    j?.stats?.filament_g ??
    (typeof summary.filament_g === "number" ? summary.filament_g : null);

  const rawName = j?.file?.filename || j?.name;
  const name = prettyName(rawName);

  // snapshot ไว้กันหน้ากระพริบ
  const snap = {
    template: template || null,
    stats: { timeMin: time_min, time_text, filament_g },
  };

  return {
    id: j.id,
    _serverId: j.id,
    _snap: snap,
    name,
    rawName: rawName || j.name,
    thumb: j.thumb || "/images/3D.png",
    template,
    stats: { timeMin: time_min, time_text, filament_g },
    file: j?.file ? {
      name,
      thumb: j?.file?.thumb || j.thumb || undefined,
      object_key: j?.file?.object_key || undefined,
      json_key: j?.file?.json_key || undefined,
    } : undefined,
    uploadedAt,
    gcode_key: gcodeKey || undefined,
    original_key: j?.original_key || undefined,
  };
}

function normalizeItem(raw) {
  if (!raw) return null;
  const name = prettyName(raw.name || raw.file?.name || raw.template?.model);
  const thumb = raw.thumb || raw.file?.thumb || raw.template?.preview || "/images/3D.png";
  const timeMin =
    raw.stats?.timeMin ??
    raw.template?.timeMin ??
    timeTextToMin(raw.stats?.time_text) ?? null;

  let template = raw.template;
  if (template && typeof template === "object" && template.settings && typeof template.settings === "object") {
    template = { ...template };
    for (const [k, v] of Object.entries(template.settings)) {
      if (template[k] == null || template[k] === "" || template[k] === 0 || template[k] === false) {
        template[k] = v;
      }
    }
    delete template.settings;
  }

  return {
    ...raw,
    id: raw.id || `${raw.uploadedAt || ""}_${name || ""}_${Math.random().toString(16).slice(2)}`,
    name: name || "Unnamed",
    thumb,
    template,
    stats: { ...(raw.stats || {}), timeMin },
    isGcode:
      !!raw.isGcode ||
      /\.(gcode|gco|gc)$/i.test(String(raw.rawName || name || "")) ||
      ["gcode", "gco", "gc"].includes(String(raw.ext || "").toLowerCase()),
    gcode_key: pickGcodeKeyLike(raw) || raw.gcode_key || null,
  };
}

// รวม/อัปเดต (อย่าเขียนทับด้วยค่าว่าง)
const prefer = (a, b) => (b !== undefined && b !== null && b !== "" ? b : a);
function mergeEntry(base, incoming) {
  const mergedStats = { ...(base.stats || {}) };
  if (incoming.stats) {
    for (const [k, v] of Object.entries(incoming.stats)) mergedStats[k] = prefer(mergedStats[k], v);
  }
  const template = prefer(base.template, incoming.template) || base._snap?.template || incoming._snap?.template || null;

  return {
    ...base,
    ...incoming,
    name: prefer(base.name, incoming.name),
    thumb: prefer(base.thumb, incoming.thumb),
    template,
    stats: mergedStats,
    file: { ...(base.file || {}), ...(incoming.file || {}) },
    gcode_key: prefer(base.gcode_key, incoming.gcode_key),
    original_key: prefer(base.original_key, incoming.original_key),
    uploadedAt: base.uploadedAt || incoming.uploadedAt || Date.now(),
    _snap: base._snap || incoming._snap || null,
    _serverId: base._serverId ?? incoming._serverId,
  };
}
function upsertList(list, incomingRaw) {
  const incoming = normalizeItem(incomingRaw);
  if (!incoming) return list;

  if (incoming._serverId != null) {
    const i = list.findIndex((it) => it && it._serverId === incoming._serverId);
    if (i >= 0) {
      const base = list[i];
      const merged = mergeEntry(base, incoming);
      const next = [...list];
      next[i] = merged;
      const [updated] = next.splice(i, 1);
      return [updated, ...next];
    }
  }

  const candKeys = new Set([incoming.original_key || "", incoming.gcode_key || ""]);
  let idx = list.findIndex((it) => it && (candKeys.has(it.original_key || "") || candKeys.has(it.gcode_key || "")));

  if (idx < 0) {
    const NEAR_MS = 20 * 1000;
    const near = (a, b) => Math.abs((a || 0) - (b || 0)) <= NEAR_MS;
    idx = list.findIndex((it) => it && it.name === incoming.name && near(it.uploadedAt, incoming.uploadedAt));
  }

  if (idx === -1) return [incoming, ...list];

  const base = list[idx];
  const merged = mergeEntry(base, incoming);
  const next = [...list];
  next[idx] = merged;
  const [updated] = next.splice(idx, 1);
  return [updated, ...next];
}

/* ---------------- component ---------------- */

export default function UserPrintHistoryModal({
  open,
  onClose,
  onPrinted,
}) {
  const { user, token } = useAuth();
  const api = useApi();

  const userId = user?.employee_id || user?.id || "anon";

  const [q, setQ] = useState("");
  const [days, setDays] = useState(0);
  const [selectedId, setSelectedId] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deletedKeys, setDeletedKeys] = useState(() => new Set());
  const [missingKeys, setMissingKeys] = useState(() => new Set()); // สำหรับ storage-only ที่หาย

  const [manifest, setManifest] = useState(null);
  const manCache = useRef(new Map()); // gcode_key -> mapped manifest

  const [gcodeStats, setGcodeStats] = useState({ grams: null });

  // ล็อกสกอลล์ + ปิดด้วย ESC
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e) => e.key === "Escape" && onClose?.();
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  // โหลดจาก LocalStorage
  const historyLocal = useMemo(() => {
    if (!open) return [];
    try {
      const arr = getUserHistory(userId)
        .map(normalizeItem)
        .filter(Boolean)
        .sort((a, b) => {
          const ta = a.uploadedAt ? new Date(a.uploadedAt).getTime() : 0;
          const tb = b.uploadedAt ? new Date(b.uploadedAt).getTime() : 0;
          return tb - ta;
        });
      return arr;
    } catch (e) {
      console.error(e);
      setErr("Failed to load your history.");
      return [];
    }
  }, [userId, open]);

  // ดึงจาก Server — งานพิมพ์ของฉัน
  useEffect(() => {
    if (!open) return;
    let alive = true;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const params = { include_processing: 1, limit: 200 };
        if (days && Number(days) > 0) params.days = Number(days);
        if (q && q.trim()) params.q = q.trim();

        const list = await api.get("/api/history/my", params, { timeoutMs: 20000 });
        if (!alive || !Array.isArray(list)) return;

        const map = readJSON(LS_HISTORY, {});
        const mine = Array.isArray(map[userId]) ? map[userId] : [];

        let merged = [...mine];
        for (const serverItem of list) merged = upsertList(merged, fromServerHistoryItem(serverItem));

        const nextMap = { ...map, [userId]: merged };
        if (!jsonEqual(map[userId], nextMap[userId])) writeJSON(LS_HISTORY, nextMap);
      } catch (e) {
        console.debug("GET /api/history/my failed:", e?.message || e);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [open, userId, days, q, api]);

  // ไฟล์ที่ฉันอัปโหลดเอง → merge
  useEffect(() => {
    if (!open) return;
    let alive = true;
    (async () => {
      try {
        const res = await api.get("/api/storage/my", { limit: 200, include_staging: 0 }, { timeoutMs: 20000 });
        const items = Array.isArray(res?.items) ? res.items : (Array.isArray(res) ? res : []);
        if (!alive || items.length === 0) return;

        const map = readJSON(LS_HISTORY, {});
        const mine = Array.isArray(map[userId]) ? map[userId] : [];
        let merged = [...mine];

        for (const f of items) {
          const key = f.object_key || f.key || null;
          const baseFromKey = key ? String(key).split("/").pop() : "";
          const rawName = f.display_name || f.original_filename || f.name || f.filename || baseFromKey || "Unnamed";
          const niceName = prettyName(rawName);
          const isGcode = /\.(gcode|gco|gc)$/i.test(String(key || rawName));
          const thumbKey = f.thumb || derivePreviewKey(key);

          const item = {
            id: `sf_${f.id}`,
            name: niceName,
            rawName,
            thumb: thumbKey || "/images/3D.png",
            file: {
              name: niceName,
              object_key: key,
              json_key: f.json_key || undefined,
            },
            uploadedAt: f.uploaded_at ? (new Date(f.uploaded_at).getTime()) : Date.now(),
            gcode_key: key,
            original_key: key,
            isGcode,
          };
          merged = upsertList(merged, item);
        }

        const nextMap = { ...map, [userId]: merged };
        if (!jsonEqual(map[userId], nextMap[userId])) writeJSON(LS_HISTORY, nextMap);
      } catch (e) {
        console.debug("GET /api/storage/my failed:", e?.message || e);
      }
    })();
    return () => { alive = false; };
  }, [open, userId, api]);

  // ซ่อนเฉพาะ storage-only items ที่ไฟล์หายไปจริง
  useEffect(() => {
    if (!open) return;
    let stopped = false;

    (async () => {
      const map = readJSON(LS_HISTORY, {});
      const mine = Array.isArray(map[userId]) ? map[userId] : [];
      const keys = [];
      for (const it of mine) {
        const k = it?.gcode_key || it?.original_key;
        if (!k) continue;
        // ถ้ามี _serverId = เป็นประวัติจาก server → เก็บไว้เสมอ แม้ไฟล์ถูกลบ
        if (it?._serverId) continue;
        if (!(k.startsWith("catalog/") || k.startsWith("storage/"))) continue;
        keys.push(k);
      }
      if (keys.length === 0) return;

      const missing = new Set();
      await Promise.all(
        keys.map(async (k) => {
          const url = `${api.API_BASE}/files/raw?object_key=${encodeURIComponent(k)}`;
          try {
            const res = await fetch(url, {
              method: "GET",
              headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), Range: "bytes=0-0" },
            });
            if (res.status === 404 || res.status === 410) missing.add(k);
          } catch {}
        })
      );
      if (stopped || !missing.size) return;

      setMissingKeys((prev) => {
        const s = new Set(prev);
        missing.forEach((m) => s.add(m));
        return s;
      });

      // ตัดออกจาก LS เฉพาะ storage-only ที่หาย
      const cur = readJSON(LS_HISTORY, {});
      const mineNow = Array.isArray(cur[userId]) ? cur[userId] : [];
      const filtered = mineNow.filter((it) => {
        const k = it?.gcode_key || it?.original_key || "";
        return !(missing.has(k) && !it?._serverId);
      });
      if (!jsonEqual(cur[userId], filtered)) writeJSON(LS_HISTORY, { ...cur, [userId]: filtered });
    })();

    return () => { stopped = true; };
  }, [open, userId, api.API_BASE, token]);

  // ฟิลเตอร์ list
  const items = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const cutoffTs = days > 0 ? Date.now() - days * 864e5 : 0;
    return historyLocal.filter((x) => {
      const key = x?.gcode_key || x?.original_key || null;
      // ตัดเฉพาะ storage-only ที่ถูกลบหรือ mark missing
      if (!x?._serverId && key && (deletedKeys.has(key) || missingKeys.has(key))) return false;

      const hitKw = !kw || (x.name || "").toLowerCase().includes(kw);
      const ts = x.uploadedAt ? new Date(x.uploadedAt).getTime() : 0;
      const hitDate = cutoffTs === 0 || ts >= cutoffTs;
      return hitKw && hitDate;
    });
  }, [q, days, historyLocal, deletedKeys, missingKeys]);

  // auto-select คง selection เดิมถ้ายังอยู่
  useEffect(() => {
    if (!open) return;
    if (selectedId && items.some((it) => it.id === selectedId)) return;
    if (!selectedId && items.length > 0) setSelectedId(items[0].id);
  }, [open, items, selectedId]);

  const selected = useMemo(
    () => items.find((x) => x.id === selectedId) || null,
    [items, selectedId]
  );

  // โหลด manifest: ใช้ cache + ไม่ clear ของเดิมขณะดึงใหม่
  const currentKey = selected?.gcode_key || pickGcodeKeyLike(selected) || "";
  useEffect(() => {
    if (!open || !currentKey) return;

    const cached = manCache.current.get(currentKey);
    if (cached) setManifest(cached);

    const candidates = guessManifestKeys(currentKey, selected?.file?.json_key);
    if (candidates.length === 0) return;

    let stop = false;
    (async () => {
      for (const key of candidates) {
        if (stop) return;
        try {
          const url = `${api.API_BASE}/files/raw?object_key=${encodeURIComponent(key)}`;
          const res = await fetch(url, {
            headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          });
          if (!res.ok) continue;
          const data = await res.json();
          const mapped = mapManifest(data);
          manCache.current.set(currentKey, mapped);
          if (!stop) setManifest(mapped);
          break;
        } catch {}
      }
    })();
    return () => { stop = true; };
  }, [open, currentKey, api.API_BASE, token, selected?.file?.json_key]);

  const manMapped = manifest || null;

  // grams fallback ถ้า manifest ไม่มี
  useEffect(() => {
    let stop = false;
    (async () => {
      const gramsAlready = manMapped?.stats?.grams;
      if (!open || !selected || gramsAlready != null || !currentKey) {
        setGcodeStats({ grams: null });
        return;
      }
      const tail = await fetchGcodeTail(api.API_BASE, currentKey, token);
      if (stop || !tail) return;
      const grams = gramsFromGcodeTail(tail);
      if (!stop) setGcodeStats({ grams: Number.isFinite(grams) ? grams : null });
    })();
    return () => { stop = true; };
  }, [open, selected, manMapped, api.API_BASE, token, currentKey]);

  // รวม template/stats ที่จะแสดง (ให้ snapshot ชนะเพื่อลดสวิง)
  const tMerged = useMemo(() => {
    const snap = selected?._snap?.template || {};
    const base = selected?.template || {};
    const fromMan = manMapped?.template || {};
    const merged = { ...fromMan, ...base, ...snap };
    if (!merged.model) {
      merged.model =
        deriveModelFromKey(currentKey || "") ||
        deriveModelFromKey(manMapped?.keys?.gcode_key || "") ||
        selected?.name ||
        "Delta";
    }
    if (merged.material) merged.material = materialShort(merged.material);
    return merged;
  }, [selected, manMapped, currentKey]);

  const statsMerged = useMemo(() => {
    const s = selected?.stats || {};
    const snap = selected?._snap?.stats || {};
    const m = manMapped?.stats || {};
    const minutes =
      toNum(s.timeMin) ??
      toNum(snap.timeMin) ??
      toNum(m.minutes);
    const grams =
      toNum(s.filament_g) ??
      toNum(snap.filament_g) ??
      toNum(m.grams) ??
      (Number.isFinite(gcodeStats.grams) ? gcodeStats.grams : null);
    const timeText =
      s.time_text || snap.time_text || m.timeText ||
      (Number.isFinite(minutes) ? fmtMinutes(minutes) : "-");
    return { minutes, grams, timeText };
  }, [selected, manMapped, gcodeStats]);

  const onImgError = useCallback((e) => {
    e.currentTarget.onerror = null;
    e.currentTarget.src = "/images/placeholder-model.png";
  }, []);

  const canReprint = !!(
    selected &&
    currentKey &&
    (selected.isGcode || /\.(gcode|gco|gc)$/i.test(String(selected?.rawName || selected?.name || "")))
  );

  const printAgain = async () => {
    if (!selected || submitting) return;
    setErr("");
    const gcode_key = currentKey;
    const isGcode =
      !!selected.isGcode ||
      /\.(gcode|gco|gc)$/i.test(String(selected.rawName || selected.name || ""));
    if (!isGcode || !gcode_key) {
      setErr("This item is not a valid G-code or missing its key.");
      return;
    }

    try {
      setSubmitting(true);

      const previewKey = manMapped?.keys?.preview_key || derivePreviewKey(gcode_key);
      const thumbUrl = resolveThumbSrc(api.API_BASE, previewKey || selected.thumb || tMerged?.preview || "", token) || null;

      const payload = {
        name: selected.rawName || selected.name || tMerged?.model || "Unnamed",
        source: "history",
        thumb: thumbUrl,
        gcode_key,
        original_key: selected.original_key || null,
        time_min: (Number.isFinite(statsMerged.minutes) ? statsMerged.minutes : undefined),
        time_text: selected.stats?.time_text ?? (Number.isFinite(statsMerged.minutes) ? fmtMinutes(statsMerged.minutes) : undefined),
        filament_g: Number.isFinite(statsMerged.grams) ? statsMerged.grams : (selected.stats?.filament_g ?? undefined),
        model: tMerged?.model ?? undefined,
        material: tMerged?.material ?? undefined,
      };

      const printerId = normalizePrinterId(
        tMerged?.printer || process.env.REACT_APP_PRINTER_ID || "prusa-core-one"
      );

      await api.post("/api/print", payload, { printer_id: printerId });

      api?.toast?.success?.("Added to print queue");
      onPrinted?.(selected);
      onClose?.();
    } catch (e) {
      console.error(e);
      setErr(e?.message || "Failed to reprint.");
    } finally {
      setSubmitting(false);
    }
  };

  const deleteFromStorage = async () => {
    if (!selected || deleting) return;
    setErr("");
    const gcode_key = currentKey;
    if (!gcode_key) {
      setErr("This item has no storage key to delete.");
      return;
    }

    const pretty = selected.name || "this file";
    const ok = window.confirm(`Delete "${pretty}" from storage?\nThis will remove the G-code and related previews/manifest.`);
    if (!ok) return;

    try {
      setDeleting(true);

      if (typeof api.delete === "function") {
        await api.delete("/api/storage/object-hard", { object_key: gcode_key }, { timeoutMs: 20000 });
      } else {
        const url = `${api.API_BASE}/api/storage/object-hard?object_key=${encodeURIComponent(gcode_key)}`;
        const res = await fetch(url, {
          method: "DELETE",
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) {
          const t = await res.text().catch(() => "");
          throw new Error(t || `Delete failed: ${res.status}`);
        }
      }

      // ตัดเฉพาะ storage-only ออกจาก LS; ประวัติจาก server (_serverId) เก็บไว้
      const map = readJSON(LS_HISTORY, {});
      const mine = Array.isArray(map[userId]) ? map[userId] : [];
      const filtered = mine.filter((it) => {
        const k1 = it?.gcode_key || "";
        const k2 = it?.original_key || "";
        if (it?._serverId) return true;
        return !(k1 === gcode_key || k2 === gcode_key);
      });
      writeJSON(LS_HISTORY, { ...map, [userId]: filtered });

      setDeletedKeys((prev) => new Set(prev).add(gcode_key));

      if (selectedId && (!filtered.find((x) => x.id === selectedId))) {
        setSelectedId(filtered.length ? filtered[0].id : null);
      }

      api?.toast?.success?.("Deleted from storage");
    } catch (e) {
      console.error(e);
      const msg = e?.message || "Delete failed.";
      if (/file_in_use_by_active_jobs/.test(msg)) {
        setErr("Cannot delete: This file is used by active jobs.");
      } else if (/forbidden/i.test(msg) || /403/.test(msg)) {
        setErr("You are not allowed to delete this file.");
      } else {
        setErr(msg);
      }
    } finally {
      setDeleting(false);
    }
  };

  const infillText = (() => {
    const t = tMerged || {};
    const v = t.infill ?? t.sparseInfillDensity ?? t.infill_percent;
    return Number.isFinite(v) ? `${v}%` : "-";
  })();

  const resolveListThumb = (it) => resolveThumbSrc(api.API_BASE, it.thumb, token);
  const resolveDetailThumb = (() => {
    const first = selected?.thumb;
    const fallback = manMapped?.keys?.preview_key || derivePreviewKey(currentKey || "");
    return resolveThumbSrc(api.API_BASE, first || fallback || "", token);
  })();

  if (!open) return null;

  return (
    <div className="uph-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="uph-modal" onClick={(e) => e.stopPropagation()}>
        <button className="uph-close" onClick={onClose} aria-label="Close">×</button>

        <div className="uph-header">
          <h2>Your Print History</h2>

          <div className="uph-controls">
            <div className="uph-filter" role="tablist" aria-label="Time filter">
              {[
                { label: "All", v: 0 },
                { label: "7d", v: 7 },
                { label: "30d", v: 30 },
                { label: "90d", v: 90 },
              ].map((btn) => (
                <button
                  key={btn.v}
                  className={`uph-chip ${days === btn.v ? "is-active" : ""}`}
                  onClick={() => setDays(btn.v)}
                  role="tab"
                  aria-selected={days === btn.v}
                >
                  {btn.label}
                </button>
              ))}
            </div>

            <div className="uph-search">
              <img src={process.env.PUBLIC_URL + "/icon/search.png"} alt="" aria-hidden="true" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search your printed files"
                aria-label="Search history"
                onKeyDown={(e) => { if (e.key === "Escape" && q) setQ(""); }}
              />
              {q && (
                <button className="uph-clear" onClick={() => setQ("")} aria-label="Clear search" title="Clear">
                  ×
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="uph-body">
          <div className="uph-list" role="listbox" aria-label="History list">
            {err && <div className="uph-empty uph-error">{err}</div>}
            {!err && loading && items.length === 0 && (
              <div className="uph-empty">Loading your history…</div>
            )}
            {!err && !loading && items.length === 0 && (
              <div className="uph-empty">No items in your history yet.</div>
            )}
            {!err &&
              items.map((item) => (
                <button
                  key={item.id}
                  className={`uph-item ${item.id === selectedId ? "is-selected" : ""}`}
                  onClick={() => setSelectedId(item.id)}
                  onKeyDown={(e) => { if (e.key === "Enter") setSelectedId(item.id); }}
                  role="option"
                  aria-selected={item.id === selectedId}
                >
                  <img className="uph-thumb" src={resolveListThumb(item)} alt="" onError={onImgError} draggable="false" />
                  <div className="uph-meta">
                    <div className="uph-name">{item.name}</div>
                    <div className="uph-sub">
                      <span>{item.template?.printer || item._snap?.template?.printer || "—"}</span>
                      <span className="uph-dot">•</span>
                      <span>{item.uploadedAt ? new Date(item.uploadedAt).toLocaleString() : ""}</span>
                    </div>
                  </div>
                </button>
              ))}
          </div>

          <div className="uph-detail">
            {selected ? (
              <>
                <div className="uph-canvas">
                  <img src={resolveDetailThumb} alt="Preview" onError={onImgError} draggable="false" />
                </div>

                <section className="uph-block">
                  <h3>Print Settings</h3>
                  <dl className="uph-dl">
                    <dt>Model</dt><dd>{tMerged?.model || selected.name}</dd>
                    <dt>Printer</dt><dd>{tMerged?.printer || "—"}</dd>
                    <dt>Sparse infill density</dt><dd>{infillText}</dd>
                    <dt>Wall loops</dt><dd>{tMerged?.wallLoops ?? "-"}</dd>
                    <dt>Support</dt><dd>{
                      (typeof tMerged?.support_mode === "string"
                        ? (SUPPORT_LABEL[tMerged.support_mode] || tMerged.support_mode)
                        : (tMerged?.supports == null ? "-" : (tMerged.supports ? "Yes" : "No"))
                      )
                    }</dd>
                    <dt>Material</dt><dd>{tMerged?.material ?? "-"}</dd>
                  </dl>
                </section>

                <section className="uph-block">
                  <h3>Slicing Result</h3>
                  <dl className="uph-dl">
                    <dt>Used Filament (g)</dt>
                    <dd>{Number.isFinite(statsMerged.grams) ? Number(statsMerged.grams).toFixed(2) : "-"}</dd>
                    <dt>Time Total</dt>
                    <dd>{statsMerged.timeText}</dd>
                  </dl>
                </section>

                <div className="uph-actions">
                  <button
                    className="uph-cta"
                    onClick={printAgain}
                    disabled={!canReprint || submitting || deleting}
                    title={canReprint ? "Print again" : "This item is not a valid G-code or missing its key"}
                  >
                    {submitting ? "Queuing..." : "Print again"}
                  </button>

                  <button
                    className="uph-danger"
                    onClick={deleteFromStorage}
                    disabled={deleting || submitting || !currentKey}
                    title="Delete this file from storage (including previews/manifest)"
                    aria-label="Delete file"
                  >
                    {deleting ? "Deleting..." : "Delete file"}
                  </button>
                </div>
              </>
            ) : (
              <div className="uph-empty">Select a print job to see details</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
