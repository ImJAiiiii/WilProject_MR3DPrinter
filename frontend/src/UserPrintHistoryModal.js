// src/UserPrintHistoryModal.js
import React, { useEffect, useMemo, useState, useRef, useCallback } from "react";
import "./UserPrintHistoryModal.css";
import { useAuth } from "./auth/AuthContext";
import { useApi } from "./api";

/* ---------------- small utils ---------------- */
const fmtMinutes = (min) => {
  if (min == null) return "-";
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};
const prettyName = (raw) => String(raw || "").replace(/\.(gcode|gco|gc)$/i, "");
const upper = (s) => (s ? String(s).toUpperCase() : "");
const isGcodeExt = (ext) => ["GCODE","GCO","GC"].includes(upper(ext));
const extFromKey = (k) => (k && String(k).match(/\.([a-z0-9]+)(?:[\?#].*)?$/i)?.[1]) || "";

const normalizePrinterId = (x) => {
  const s = String(x || "").trim().toLowerCase();
  if (!s) return "prusa-core-one";
  const z = s.replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/--+/g, "-").replace(/^-+|-+$/g, "");
  if (z.startsWith("prusa") && z.includes("core") && z.includes("one")) return "prusa-core-one";
  return z || "prusa-core-one";
};
const sanitizeKey = (k) => {
  if (!k) return "";
  let v = String(k).trim();
  if (v.startsWith("printer-store/")) v = v.slice("printer-store/".length);
  v = v.replace("/HONTECH/","/Hontech/").replace("/DELTA/","/Delta/");
  return v.replace(/^\/+/, "");
};
const stripGcodeExt = (k) => String(k || "").replace(/\.(gcode|gco|gc)$/i, "");

/* ---------------- derive gcode key ---------------- */
const parseMaybeJson = (x) => {
  if (!x) return null;
  if (typeof x === "object") return x;
  try { return JSON.parse(String(x)); } catch { return null; }
};
function* deepStrings(obj, depth = 0) {
  if (!obj || depth > 6) return;
  if (typeof obj === "string") { yield obj; return; }
  if (Array.isArray(obj)) { for (const v of obj) yield* deepStrings(v, depth + 1); return; }
  if (typeof obj === "object") {
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      if (typeof v === "string") yield v;
      else yield* deepStrings(v, depth + 1);
    }
  }
}
const findAnyGcodeString = (obj) => {
  const rx = /\.(gcode|gco|gc)(?:[\?#].*)?$/i;
  for (const s of deepStrings(obj)) if (rx.test(s)) return s;
  return null;
};

/* ✅ เดาจาก thumb/preview → .gcode (เหมือนฝั่ง Custom Store) */
function guessGcodeFromThumb(thumb, nameFallback) {
  if (!thumb) return null;
  const t = sanitizeKey(thumb);
  // แกน: ตัด suffix preview/thumb + นามสกุล → เติม .gcode
  const base = t
    .replace(/\.(preview|thumb)\.(png|jpg|jpeg)$/i, "")
    .replace(/_(preview|thumb)\.(png|jpg|jpeg)$/i, "")
    .replace(/_oriented(\.(preview|thumb))?\.(png|jpg|jpeg)$/i, "")
    .replace(/(\.png|\.jpg|\.jpeg)$/i, "");

  const folder = base.includes("/") ? base.slice(0, base.lastIndexOf("/") + 1) : "";
  const stem   = base.includes("/") ? base.slice(base.lastIndexOf("/") + 1) : base;

  const stems = new Set([
    stem,
    stem.replace(/_oriented$/i, ""),
    (nameFallback || "").trim() || stem.replace(/_oriented$/i, "")
  ]);

  const variants = [];
  for (const s of stems) {
    const b = `${folder}${s}`;
    variants.push(`${b}.gcode`, `${b}.GCODE`, `${b}.gco`, `${b}.gc`);
    // space/plus สลับ (กันกรณีเก็บต่างรูปแบบ)
    variants.push(`${folder}${s.replace(/\+/g, " ")}.gcode`);
    variants.push(`${folder}${s.replace(/ /g, "+")}.gcode`);
  }
  // ลองเติม _oriented เผื่อเก็บชื่อไฟล์จริงแบบนั้น
  for (const s of stems) {
    const b = `${folder}${s.endsWith("_oriented") ? s : `${s}_oriented`}`;
    variants.push(`${b}.gcode`, `${b}.GCODE`);
  }
  return Array.from(new Set(variants));
}

function deriveGcodeKey(j) {
  const file = parseMaybeJson(j.file) || parseMaybeJson(j.file_json);
  const stats = parseMaybeJson(j.stats_json);
  const payload = parseMaybeJson(j.payload);
  const extra = parseMaybeJson(j.extra) || parseMaybeJson(j.meta);

  const thumbRaw = file?.thumb || file?.preview_key || j.thumb || null;

  const cands = [
    j.gcode_key, j.gcode_path, j.object_key, j.original_key,
    file?.gcode_key, file?.gcode_path, file?.object_key, file?.key, file?.path,
    payload?.gcode_key, payload?.gcode_path, payload?.object_key,
    stats?.gcode_key, stats?.object_key,
    extra?.gcode_key, extra?.object_key,
    j?._raw?.object_key,
  ].filter(Boolean).map(sanitizeKey);

  // 1) เลือกที่ลงท้าย .gcode ก่อน
  for (const k of cands) if (isGcodeExt(extFromKey(k))) return k;

  // 2) เดาจาก thumb/preview → .gcode (+ ชื่อไฟล์ job เป็น fallback)
  const fromThumb = guessGcodeFromThumb(thumbRaw, j?.name || file?.filename);
  for (const k of fromThumb || []) return k; // ไม่ต้อง validate ที่นี่ ให้ backendเช็ค

  // 3) หาแบบ recursive ใน JSON fields
  const deep = findAnyGcodeString({ j, file, stats, payload, extra });
  if (deep) return sanitizeKey(deep);

  // 4) ไม่เจอ ก็คืนตัวแรกไว้ก่อน (บางระบบ key ไม่มีนามสกุล)
  return cands.length ? cands[0] : null;
}

/* ---------------- thumb + presign ---------------- */
function guessThumbCandidates(thumb, gcodeKey) {
  const list = [];
  if (thumb && !thumb.startsWith("/images/")) list.push(sanitizeKey(thumb));
  if (gcodeKey) {
    const base = stripGcodeExt(sanitizeKey(gcodeKey));
    const baseNo = base.replace("_oriented", "");
    const bases = [base, baseNo, `${baseNo}_oriented`];
    const pats = [
      "{b}.preview.png","{b}.preview.jpg","{b}.preview.jpeg",
      "{b}_preview.png","{b}_preview.jpg","{b}_preview.jpeg",
      "{b}_thumb.png","{b}_thumb.jpg","{b}_thumb.jpeg",
      "{b}_oriented.preview.png","{b}_oriented.preview.jpg","{b}_oriented.preview.jpeg",
      "{b}_oriented_preview.png","{b}_oriented_preview.jpg","{b}_oriented_preview.jpeg",
    ];
    for (const b of bases) for (const p of pats) list.push(p.replace("{b}", b));
  }
  const extra = [];
  for (const k of list) {
    if (k.includes("/Hontech/")) { extra.push(k.replace("/Hontech/","/HONTECH/")); extra.push(k.replace("/Hontech/","/Hontec/")); }
    if (k.includes("/HONTECH/")) extra.push(k.replace("/HONTECH/","/Hontech/"));
    if (k.includes("/Delta/"))   extra.push(k.replace("/Delta/","/DELTA/"));
    if (k.includes("/DELTA/"))   extra.push(k.replace("/DELTA/","/Delta/"));
  }
  return Array.from(new Set([...list, ...extra]));
}
const presignCache = new Map();
const buildPresignUrl = (apiBase, key) => {
  const u = new URL(`${apiBase}/api/storage/presign`); u.searchParams.set("object_key", sanitizeKey(key)); return u.toString();
};
function SmartImg({ apiBase, token, thumb, gcodeKey, alt = "", className = "" }) {
  const [idx, setIdx] = useState(0);
  const [src, setSrc] = useState("");
  const candidates = useMemo(() => guessThumbCandidates(thumb, gcodeKey), [thumb, gcodeKey]);
  useEffect(() => { setSrc(""); setIdx(0); }, [thumb, gcodeKey]);
  useEffect(() => {
    let aborted = false;
    (async () => {
      const cand = candidates[idx]; if (!cand) return;
      if (/^https?:\/\//i.test(cand)) { setSrc(cand); return; }
      const clean = sanitizeKey(cand);
      if (presignCache.has(clean)) { setSrc(presignCache.get(clean)); return; }
      try {
        const res = await fetch(buildPresignUrl(apiBase, clean), { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!res.ok) throw new Error(`presign ${res.status}`);
        let finalUrl = null;
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) finalUrl = (await res.json())?.url || null;
        else finalUrl = (await res.text())?.replace(/^"+|"+$/g, "") || null;
        if (!finalUrl) throw new Error("empty presign");
        presignCache.set(clean, finalUrl);
        if (!aborted) setSrc(finalUrl);
      } catch {
        if (!aborted && idx < candidates.length - 1) setIdx(idx + 1);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idx, candidates, apiBase, token]);
  return <img src={src || "/images/3D.png"} alt={alt} className={className} onError={() => idx < candidates.length - 1 && setIdx(idx + 1)} draggable="false" />;
}

/* ---------------- map server → UI ---------------- */
function fromServerHistoryItem(j) {
  const uploadedAt =
    j.finished_at ? new Date(j.finished_at).getTime()
    : j.uploaded_at ? new Date(j.uploaded_at).getTime()
    : Date.now();

  const file = parseMaybeJson(j.file) || parseMaybeJson(j.file_json) || {};
  const thumbServer = file.thumb || file.preview_key || j.thumb || null;

  const gcodeKey = deriveGcodeKey({ ...j, file }) || guessGcodeFromThumb(thumbServer, j?.name)?.[0] || null;

  const name = prettyName(file.filename || j?.name || (gcodeKey ? gcodeKey.split("/").pop() : "(Unnamed)"));
  const stats = parseMaybeJson(j.stats_json) || j.stats || {};
  const time_min = j.time_min ?? stats.time_min ?? stats.estimate_min ?? null;
  const filament_g = j.filament_g ?? stats.filament_g ?? stats.filament_g_total ?? null;
  const time_text = stats.time_text ?? (Number.isFinite(time_min) ? fmtMinutes(time_min) : "-");

  return {
    id: j.id,
    name,
    rawName: j.name,
    status: j.status,
    source: j.source || "history",
    printer_id: j.printer_id || "prusa-core-one",
    thumb: thumbServer,
    uploadedAt,
    gcode_key: gcodeKey && String(gcodeKey).trim() ? gcodeKey : null,
    stats: { timeMin: time_min, time_text, filament_g },
  };
}

/* ---------------- single-flight (global) ---------------- */
const getSF = () => {
  const w = window;
  if (!w.__PRINT_SF__) w.__PRINT_SF__ = new Map(); // key -> Promise
  return w.__PRINT_SF__;
};

/* ============================== Component ============================== */
export default function UserPrintHistoryModal({ open, onClose }) {
  const { user, token } = useAuth();
  const api = useApi();

  const [q, setQ] = useState("");
  const [days, setDays] = useState(0);
  const [selectedId, setSelectedId] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [historyList, setHistoryList] = useState([]);

  const lockRef = useRef(false);

  /* load history */
  useEffect(() => {
    if (!open) return;
    let alive = true;
    (async () => {
      setLoading(true); setErr("");
      try {
        const params = { include_processing: 1, limit: 200 };
        if (days > 0) params.days = days;
        if (q.trim()) params.q = q.trim();
        const res = await api.get("/api/history/my", params, { timeoutMs: 20000 });
        if (!alive) return;
        const list = Array.isArray(res) ? res : Array.isArray(res?.items) ? res.items : [];
        const normalized = list
          .map(fromServerHistoryItem)
          .filter((it) => ["octoprint","upload","storage","history"].includes((it.source || "").toLowerCase()))
          .sort((a,b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));
        setHistoryList(normalized);
        setSelectedId(normalized[0]?.id ?? null);
      } catch (e) {
        console.error("GET /api/history/my failed:", e); setErr("Failed to load history from server.");
      } finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, [open, days, q, api]);

  const items = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const cut = days > 0 ? Date.now() - days * 864e5 : 0;
    return historyList.filter(x => (!kw || (x.name || "").toLowerCase().includes(kw)) && (cut === 0 || (+x.uploadedAt || 0) >= cut));
  }, [q, days, historyList]);

  const selected = useMemo(() => items.find(x => x.id === selectedId) || null, [items, selectedId]);

  const idemKey = useMemo(() => {
    if (!selected) return `reprint:unknown:${Date.now()}`;
    const pid = normalizePrinterId(selected.printer_id || process.env.REACT_APP_PRINTER_ID || "prusa-core-one");
    return `reprint:${pid}:${selected.gcode_key || "unknown"}`;
  }, [selected]);

  const printAgain = useCallback(async () => {
    if (!selected || submitting || lockRef.current) return;
    setErr("");

    const gcode_key = selected.gcode_key;
    if (!gcode_key) { setErr("This item is missing its G-code key."); return; }

    const pid = normalizePrinterId(selected.printer_id || process.env.REACT_APP_PRINTER_ID || "prusa-core-one");
    const payload = {
      name: selected.rawName || selected.name || "Unnamed",
      source: "history",
      thumb: selected.thumb ?? undefined,
      gcode_key,
      original_key: gcode_key,
      time_min: selected?.stats?.timeMin ?? undefined,
      filament_g: selected?.stats?.filament_g ?? undefined,
    };

    try {
      lockRef.current = true;
      setSubmitting(true);

      const sf = getSF();
      const key = idemKey;
      if (sf.has(key)) { await sf.get(key); return; }
      const p = api.post(
        "/api/print",
        payload,
        { printer_id: pid },
        { timeoutMs: 15000, headers: { "Idempotency-Key": idemKey } }
      );
      sf.set(key, p);
      await p;
      sf.delete(key);

      api?.toast?.success?.("Added to print queue");
      onClose?.();
    } catch (e) {
      console.error(e);
      setErr(e?.message || "Failed to reprint.");
    } finally {
      setSubmitting(false);
      setTimeout(() => { lockRef.current = false; }, 300);
    }
  }, [api, idemKey, onClose, selected, submitting]);

  if (!open) return null;

  return (
    <div className="uph-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="uph-modal" onClick={(e) => e.stopPropagation()}>
        <button className="uph-close" onClick={onClose} aria-label="Close">×</button>

        <div className="uph-header">
          <h2>Your Print History</h2>
          <div className="uph-controls">
            <div className="uph-filter">
              {[{label:"All",v:0},{label:"7d",v:7},{label:"30d",v:30},{label:"90d",v:90}].map(btn=>(
                <button key={btn.v}
                        className={`uph-chip ${days===btn.v?"is-active":""}`}
                        onClick={()=>setDays(btn.v)}>{btn.label}</button>
              ))}
            </div>
            <div className="uph-search">
              <input value={q} onChange={(e)=>setQ(e.target.value)} placeholder="Search your printed files" />
              {q && <button className="uph-clear" onClick={()=>setQ("")}>×</button>}
            </div>
          </div>
        </div>

        <div className="uph-body">
          <div className="uph-list">
            {err && <div className="uph-empty uph-error">{err}</div>}
            {!err && loading && items.length===0 && <div className="uph-empty">Loading your history…</div>}
            {!err && !loading && items.length===0 && <div className="uph-empty">No print history yet.</div>}
            {!err && items.map((item) => (
              <button key={item.id}
                      className={`uph-item ${item.id===selectedId ? "is-selected":""}`}
                      onClick={()=>setSelectedId(item.id)}>
                <SmartImg apiBase={api.API_BASE} token={token} thumb={item.thumb} gcodeKey={item.gcode_key} className="uph-thumb" />
                <div className="uph-meta">
                  <div className="uph-name">{item.name}</div>
                  <div className="uph-sub">
                    <span>{item.source}</span><span className="uph-dot">•</span>
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
                <SmartImg apiBase={api.API_BASE} token={token} thumb={selected.thumb} gcodeKey={selected.gcode_key} className="uph-preview" />
              </div>

              <section className="uph-block">
                <h3>Job Info</h3>
                <dl className="uph-dl">
                  <dt>Name</dt><dd>{selected.name}</dd>
                  <dt>Status</dt><dd>{selected.status}</dd>
                  <dt>Source</dt><dd>{selected.source}</dd>
                  <dt>Filament (g)</dt><dd>{selected.stats?.filament_g ?? "-"}</dd>
                  <dt>Time</dt><dd>{selected.stats?.time_text ?? "-"}</dd>
                </dl>
              </section>

              <button
                type="button"
                className={`uph-cta${submitting ? " is-busy" : ""}`}
                onClick={printAgain}
                disabled={submitting || !selected.gcode_key}
                title={!selected.gcode_key ? "This item is missing its G-code key." : "Print again"}
              >
                {submitting ? "Queuing..." : "Print again"}
              </button>
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
