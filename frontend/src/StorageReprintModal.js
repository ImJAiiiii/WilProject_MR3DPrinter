// src/StorageReprintModal.js
import React, { useEffect, useMemo, useState, useCallback, useRef } from "react";
import "./StorageReprintModal.css";
import { useApi } from "./api";
import { useAuth } from "./auth/AuthContext";

/* ------------ helpers (pure) ------------ */
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
const toNumber = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const fmtHM = (min) => {
  if (!Number.isFinite(min)) return "-";
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};
const fmtDateTime = (v) => {
  try {
    if (!v && v !== 0) return "-";
    const d = typeof v === "number" ? new Date(v) : new Date(v);
    return isNaN(d) ? "-" : d.toLocaleString();
  } catch { return "-"; }
};
const fmtBytes = (n) => {
  const b = Number(n);
  if (!Number.isFinite(b)) return "-";
  if (b >= 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
};
const upper = (s) => (s ? String(s).toUpperCase() : "");
const isGcodeExt = (ext) => ["GCODE", "GCO", "GC"].includes(upper(ext));
const extFromKey = (k) => {
  if (!k) return "";
  const m = String(k).match(/\.([a-z0-9]+)$/i);
  return m ? m[1] : "";
};

/* ---- manifest mapping helpers ---- */
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const fromPercent = (s) => {
  if (s == null) return null;
  const m = String(s).match(/([\d.]+)/);
  return m ? num(m[1]) : null;
};
const guessNozzle = (s) => {
  if (!s) return null;
  const m =
    String(s).match(/(\d+(?:\.\d+)?)\s*mm/i) || String(s).match(/(\d\.\d)/);
  return m ? num(m[1]) : null;
};
const deriveModelFromKey = (key) => {
  try {
    if (!key) return null;
    const parts = String(key).split("/");
    return parts[0] === "catalog" && parts[1] ? parts[1] : null;
  } catch { return null; }
};

/* ==== material + grams ==== */
function simplifyMaterial(s) {
  if (!s) return null;
  const u = String(s).toUpperCase();
  const picks = ["PLA","PETG","ABS","TPU","ASA","PA","NYLON","PC","PCTG","HIPS","PP"];
  for (const k of picks) if (u.includes(k)) return k;
  const w = (u.match(/[A-Z]{2,}/g) || [])[0];
  return w || s;
}
function mmToGrams(lenMm, dia = 1.75, density = 1.24) {
  const L = Number(lenMm);
  if (!Number.isFinite(L) || L <= 0) return null;
  const r = dia / 2 / 10; // mm -> cm
  const area = Math.PI * r * r; // cm^2
  const vol = area * (L / 10); // cm^3
  return +(vol * density).toFixed(2);
}
const parseNumInText = (s) => {
  if (s == null) return null;
  const m = String(s).match(/-?\d+(?:\.\d+)?/);
  return m ? num(m[0]) : null;
};
function pickFilamentGrams(man) {
  const s = man?.summary || {};
  const direct =
    num(s.filament_g) ?? num(s.filament_g_total) ?? num(s.filament_total_g) ??
    num(man.filament_g) ?? num(man.filament_g_total) ?? num(man.filament_total_g) ??
    num(s.filament?.g) ?? num(s.filament?.grams) ?? num(s.filament?.total_g);
  if (direct != null) return +direct.toFixed(2);

  const arr = Array.isArray(man.filament) ? man.filament
            : Array.isArray(man.filaments) ? man.filaments
            : Array.isArray(man.extruders) ? man.extruders
            : null;
  if (arr && arr.length) {
    const sum = arr.reduce((acc, it) => {
      const g = num(it?.g) ?? num(it?.grams) ?? parseNumInText(it?.text);
      return acc + (g || 0);
    }, 0);
    if (sum > 0) return +sum.toFixed(2);
  }

  const text = s.filament_text ?? s.filament ?? man.filament_text ?? man.filament;
  const parsed = parseNumInText(text);
  if (parsed != null && parsed > 0) return +parsed.toFixed(2);

  const mm = num(s.filament_mm) ?? num(s.filament_total_mm) ?? num(man.filament_mm) ?? num(man.filament_total_mm);
  const gFromMm = mmToGrams(mm);
  if (gFromMm != null) return gFromMm;

  return null;
}

/* ===== fallback parse from G-code tail ===== */
const rxG = [
  /filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g/i,
  /filament\s*used\s*\[g\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
  /filament[_\s]*total[_\s]*g[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
  /total\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g/i,
  /filament\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g/i,
  /estimated\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g/i,
];
const rxMM = [
  /filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*mm/i,
  /filament\s*used\s*\[mm\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
  /filament[_\s]*total[_\s]*mm[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*mm/i,
];
const parseFilamentFromText = (txt) => {
  const _to = (s) => Number(String(s).replace(/,/g, ""));
  for (const r of rxG) {
    const m = txt.match(r);
    if (m) return _to(m[1]);
  }
  for (const r of rxMM) {
    const m = txt.match(r);
    if (m) {
      const mm = _to(m[1]);
      return mmToGrams(mm);
    }
  }
  return null;
};

/* ==== mapping manifest -> view model ==== */
const mapManifest = (man = {}) => {
  const summary = man.summary || {};
  const applied = man.applied || {};
  const presets = (man.slicer && man.slicer.presets) || {};

  const template = {
    profile: presets.print || null,
    printer: presets.printer || null,
    material: simplifyMaterial(presets.filament || man.material || ""),
    layer: num(applied.first_layer_height ?? applied.layer_height),
    nozzle: num(applied.nozzle) ?? guessNozzle(presets.printer),
    infill: fromPercent(applied.fill_density),
    supports: applied.support ? String(applied.support).toLowerCase() !== "none" : null,
    model: deriveModelFromKey(man.gcode_key) || null,
  };

  const minutes = num(summary.estimate_min);
  const grams = pickFilamentGrams(man);

  const stats = {
    minutes,
    timeText: summary.total_text || (minutes != null ? fmtHM(minutes) : null),
    grams,
  };

  const keys = {
    preview_key: man.preview_key || null,
    gcode_key: man.gcode_key || null,
  };
  return { template, stats, keys };
};

/* ---- URL helpers ---- */
function joinUrl(base, path) {
  try {
    const b = String(base || "").trim();
    const p = String(path || "");
    const origin = (typeof window !== "undefined" && window.location?.origin) || "";
    return new URL(p, b ? (b.endsWith("/") ? b : b + "/") : origin + "/").toString();
  } catch { return path; }
}
function withToken(u, tkn) {
  if (!tkn) return u;
  try { const url = new URL(u); url.searchParams.set("token", tkn); return url.toString(); }
  catch { const sep = u.includes("?") ? "&" : "?"; return `${u}${sep}token=${encodeURIComponent(tkn)}`; }
}
function toRawUrl(apiBase, objectKey, token) {
  const path = `/files/raw?object_key=${encodeURIComponent(objectKey)}`;
  return withToken(joinUrl(apiBase, path), token);
}

/* ---- Preview candidate ---- */
function derivePreviewCandidatesFromKey(keyWithExt) {
  if (!keyWithExt) return [];
  const dot = keyWithExt.lastIndexOf(".");
  const base = dot >= 0 ? keyWithExt.slice(0, dot) : keyWithExt;
  const spaceBase = base.replace(/\+/g, " ");
  const plusBase  = base.replace(/ /g, "+");
  const pack = (b) => ([
    `${b}.preview.png`,   `${b}_preview.png`,
    `${b}.preview.jpg`,   `${b}_preview.jpg`,
    `${b}.preview.jpeg`,  `${b}_preview.jpeg`,
  ]);
  return plusBase !== spaceBase ? [...pack(spaceBase), ...pack(plusBase)] : pack(spaceBase);
}

/* ---- Fallback icon (SVG data-uri) ---- */
const FALLBACK_ICON =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120" viewBox="0 0 128 96" aria-hidden="true">
      <rect x="8" y="8" width="112" height="80" rx="8" ry="8" fill="#eef1f5" stroke="#c9d0d8"/>
      <path d="M20 74 L48 46 L68 60 L88 40 L108 74 Z" fill="#d6dde6" stroke="#aab6c3"/>
      <circle cx="40" cy="34" r="8" fill="#c1cddd"/>
    </svg>`
  );

export default function StorageReprintModal({ open, file, onClose, onPrint }) {
  const api = useApi();
  const { token } = useAuth();

  const f = file || {};
  const baseTpl = f.template || f._raw?.template || {};

  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");
  const [manifest, setManifest] = useState(null);
  const [manLoaded, setManLoaded] = useState(false);
  const [gramsFromGcode, setGramsFromGcode] = useState(null);

  // 🔒 กันยิงซ้ำแบบ synchronous
  const lockRef = useRef(false);
  const lastEnterAtRef = useRef(0);

  /* reset on open/file change */
  useEffect(() => {
    setManifest(null);
    setManLoaded(false);
    setErr("");
    setGramsFromGcode(null);
    lockRef.current = false; // reset lock เมื่อ modal เปิดใหม่
  }, [open, file]);

  /* ---- load manifest lazily ---- */
  const jsonKeyFromGcodeKey = (k) => (k ? String(k).replace(/\.(gcode|gco|gc)$/i, ".json") : null);
  useEffect(() => {
    let stop = false;

    const jsonKeyInitial = f._raw?.json_key || f.json_key;
    const gkFallback =
      f.gcode_key || f.object_key || f._raw?.object_key || f.file?.object_key || null;
    const jsonKey = jsonKeyInitial || jsonKeyFromGcodeKey(gkFallback);

    const load = async () => {
      if (!open || !jsonKey || manLoaded) return;
      try {
        const url = `${api.API_BASE}/files/raw?object_key=${encodeURIComponent(jsonKey)}&_ts=${Date.now()}`;
        const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : undefined });
        if (res.status === 404) { if (!stop) setManLoaded(true); return; }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!stop) { setManifest(data); setManLoaded(true); }
      } catch (e) {
        console.warn("manifest load failed:", e);
        if (!stop) setManLoaded(true);
      }
    };
    load();
    return () => { stop = true; };
  }, [open, file, api.API_BASE, token, manLoaded]);

  const manMapped = useMemo(() => (manifest ? mapManifest(manifest) : null), [manifest]);

  /* ---- keys / readiness ---- */
  let gcodeKey =
    f.gcode_key || f.object_key || f._raw?.object_key || f.file?.object_key || baseTpl.gcode_key || null;
  if (manMapped?.keys?.gcode_key) gcodeKey = manMapped.keys.gcode_key;

  const extNow = upper(f.ext || extFromKey(f.object_key || f._raw?.object_key || gcodeKey));
  const isGcode = Boolean(f.isGcode || isGcodeExt(extNow));
  const isReady = isGcode && !!gcodeKey;

  /* ---- fallback grams from gcode tail ---- */
  useEffect(() => {
    let stop = false;
    const needFallback = open && isReady && (manMapped?.stats?.grams == null || Number(manMapped?.stats?.grams) === 0);
    if (!needFallback) { setGramsFromGcode(null); return; }

    (async () => {
      try {
        const url = `${api.API_BASE}/api/storage/range?object_key=${encodeURIComponent(
          gcodeKey
        )}&start=-4000000&length=4000000&_ts=${Date.now()}`;
        const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : undefined });
        if (res.status === 404) { if (!stop) setGramsFromGcode(null); return; }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const txt = await res.text();
        if (stop) return;
        const g = parseFilamentFromText(txt);
        setGramsFromGcode(Number.isFinite(g) && g > 0 ? +g.toFixed(2) : null);
      } catch (e) {
        console.warn("fallback parse from G-code failed:", e);
        if (!stop) setGramsFromGcode(null);
      }
    })();

    return () => { stop = true; };
  }, [open, isReady, api.API_BASE, token, gcodeKey, manMapped?.stats?.grams]);

  /* ---- merged template (manifest wins) ---- */
  const t = useMemo(
    () => ({
      ...(manMapped?.template || {}),
      ...(baseTpl || {}),
      model:
        manMapped?.template?.model ??
        baseTpl.model ??
        deriveModelFromKey(f.object_key || f._raw?.object_key) ??
        undefined,
    }),
    [baseTpl, manMapped, f.object_key, f._raw]
  );

  /* ---- stats ---- */
  const storedStats = useMemo(() => {
    const s = f.stats || f._raw?.stats || {};
    const grams = toNumber(s.filament_g_total ?? s.filament_g);
    const minutes = Number.isFinite(s.est_time_min ?? s.time_min)
      ? Math.round(s.est_time_min ?? s.time_min) : null;
    const timeText = s.time_text || (minutes != null ? fmtHM(minutes) : null);
    if (grams != null || minutes != null || timeText) return { grams, minutes, timeText };

    const gramsTplRaw =
      toNumber(baseTpl.filament_g) ?? toNumber(baseTpl.filamentGrams) ??
      toNumber(baseTpl.usedFilament) ?? toNumber(baseTpl.filament);
    const gramsTpl = gramsTplRaw && gramsTplRaw > 0 ? gramsTplRaw : null;

    const sec = toNumber(baseTpl.timeSec) ?? toNumber(baseTpl.printTimeSec) ?? toNumber(baseTpl.slicer_time_sec);
    const minRaw = toNumber(baseTpl.timeMin) ?? toNumber(baseTpl.printTimeMin) ?? toNumber(baseTpl.slicer_time_min);
    const totalMin = Number.isFinite(sec) ? Math.round(sec / 60) : Number.isFinite(minRaw) ? Math.round(minRaw) : null;

    return { grams: gramsTpl ?? null, minutes: totalMin, timeText: totalMin != null ? fmtHM(totalMin) : null };
  }, [f.stats, f._raw, baseTpl]);

  const estimate = useMemo(() => {
    if (manMapped?.stats) return manMapped.stats;
    if (!isReady) return { grams: null, minutes: null, timeText: null };
    const baseG = 40, baseMin = 90;
    const layer = Number((baseTpl.layer ?? t.layer ?? 0.2)) || 0.2;
    const infill = Number((baseTpl.infill ?? t.infill ?? t.sparseInfillDensity ?? 15)) || 15;
    const supports = !!(baseTpl.supports ?? t.supports);
    const layerFactor = 0.2 / layer;
    const infillFactor = (10 + infill) / 25;
    const supportFactor = supports ? 1.15 : 1;
    const grams = Math.round(baseG * infillFactor * supportFactor);
    const minutes = Math.round(baseMin * layerFactor * infillFactor * supportFactor);
    return { grams, minutes, timeText: fmtHM(minutes) };
  }, [isReady, manMapped, baseTpl, t]);

  const gramsDisplay =
    gramsFromGcode != null
      ? gramsFromGcode
      : manMapped?.stats?.grams ?? storedStats.grams ?? estimate.grams;
  const usedGramsText = gramsDisplay != null ? `${Number(gramsDisplay).toFixed(2)} g` : "-";
  const timeText =
    manMapped?.stats?.timeText ?? storedStats.timeText ?? estimate.timeText ?? "-";
  const timeLabel =
    manMapped?.stats?.timeText || storedStats.timeText ? "Time" : "Estimated Time";

  const uploadedDisplay = f.uploadedAt || fmtDateTime(f.uploadedTs) || "-";
  const sizeDisplay = f.sizeText || (Number.isFinite(f.size) ? fmtBytes(f.size) : "-");

  // ---- Build preview URL ----
  const previewSrc = useMemo(() => {
    const manPreview = manMapped?.keys?.preview_key;
    if (manPreview) return toRawUrl(api.API_BASE, manPreview, token);

    if (typeof f.thumb === "string") {
      if (/^https?:\/\//i.test(f.thumb) || f.thumb.startsWith("data:")) return f.thumb;
      return toRawUrl(api.API_BASE, f.thumb, token);
    }

    const keyBase =
      f.gcode_key || f.object_key || f._raw?.object_key || f.file?.object_key || (manMapped?.keys?.gcode_key) || "";
    if (keyBase) {
      const cand = derivePreviewCandidatesFromKey(keyBase)[0];
      if (cand) return toRawUrl(api.API_BASE, cand, token);
    }
    return FALLBACK_ICON;
  }, [api.API_BASE, token, f.thumb, f.gcode_key, f.object_key, f._raw, f.file, manMapped]);

  // ---------- idempotency key ----------
  const idemKey =
    (t.printer ? normalizePrinterId(t.printer) : "prusa-core-one") && gcodeKey
      ? `reprint:${normalizePrinterId(t.printer || "prusa-core-one")}:${gcodeKey}`
      : `reprint:unknown:${Date.now()}`;

  /* ---- submit ---- */
  const submit = useCallback(async () => {
    // เช็คทั้ง isReady, submitting และ lockRef (กันดับเบิลคลิก/Enter)
    if (!isReady || submitting || lockRef.current) return;
    lockRef.current = true;

    setErr("");
    setSubmitting(true);
    try {
      const payload = {
        name: f.name || t.name || "Unnamed",
        source: "storage",
        thumb: previewSrc || null,
        gcode_key: gcodeKey,
        original_key: f.original_key || null,
        time_min:
          (manMapped?.stats?.minutes ?? storedStats.minutes ?? estimate.minutes) ?? undefined,
        time_text: timeText ?? undefined,
        filament_g: gramsDisplay ?? undefined,
        model: t.model ?? undefined,
        material: t.material ?? undefined,
      };

      const printerId = normalizePrinterId(
        t.printer || process.env.REACT_APP_PRINTER_ID || "prusa-core-one"
      );

      await api.post(
        "/api/print",
        payload,
        { printer_id: printerId },
        { timeoutMs: 15000, headers: { "Idempotency-Key": idemKey } }
      );

      onPrint?.(payload);
      try {
        window.dispatchEvent(new CustomEvent("toast", { detail: { type: "success", text: "Queued to printer" } }));
      } catch {}
      onClose?.();
    } catch (e) {
      console.error(e);
      setErr(e?.message || "Failed to reprint.");
    } finally {
      setSubmitting(false);
      // ปลดล็อกหลังจบเฟรม (delay เล็กน้อยกัน event ซ้อน)
      setTimeout(() => { lockRef.current = false; }, 300);
    }
  }, [
    api, estimate.minutes, f.name, f.original_key, gcodeKey, gramsDisplay, idemKey,
    isReady, manMapped?.stats?.minutes, onClose, onPrint, previewSrc,
    storedStats.minutes, submitting, t.material, t.model, t.name, t.printer, timeText,
  ]);

  /* ---- lifecycle / a11y ---- */
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose?.(); return; }
      if (e.key === "Enter") {
        const now = Date.now();
        if (now - lastEnterAtRef.current < 1200) return;
        lastEnterAtRef.current = now;
        e.preventDefault();
        if (!submitting && !lockRef.current) submit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = prev; window.removeEventListener("keydown", onKey); };
  }, [open, onClose, submit, submitting]);

  if (!open || !file) return null;

  return (
    <div className="rp-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <div className="rp-modal" onClick={(e) => e.stopPropagation()}>
        <button className="rp-close" onClick={onClose} aria-label="Close">×</button>

        <div className="rp-header">
          <h2 className="rp-title">Reprint from Storage</h2>
          <div className="rp-file" title={f.name || ""}>
            <span className="rp-chip">{f.name || "-"}</span>
          </div>
        </div>

        <div className="rp-body">
          {/* left: preview + file info */}
          <div className="rp-preview">
            <div className="rp-canvas">
              <img
                src={previewSrc}
                alt="Model preview"
                onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = FALLBACK_ICON; }}
                loading="lazy"
                decoding="async"
              />
            </div>

            <div className="rp-block">
              <h3>File Info</h3>
              <dl className="rp-dl">
                <dt>Uploaded</dt><dd>{uploadedDisplay}</dd>
                <dt>Size</dt><dd>{sizeDisplay}</dd>
                <dt>Type</dt><dd>{isGcode ? "G-code" : upper(f.ext) || "-"}</dd>
                {f.uploader && (<><dt>Uploader</dt><dd>{f.uploader}</dd></>)}
              </dl>
            </div>
          </div>

          {/* right: template & summary */}
          <div className="rp-form rp-form--readonly">
            <div className="rp-block">
              <h3>Print Template</h3>
              <dl className="rp-dl">
                <dt>Profile</dt><dd>{t.profile || "-"}</dd>
                <dt>Model</dt><dd>{t.model || f.model || "-"}</dd>
                <dt>Printer</dt><dd>{t.printer || "-"}</dd>
                {t.material != null && (<><dt>Material</dt><dd>{t.material}</dd></>)}
                {t.layer != null && (<><dt>Layer height</dt><dd>{t.layer} mm</dd></>)}
                <dt>Infill</dt>
                <dd>{t.infill != null ? `${t.infill}%` : t.sparseInfillDensity != null ? `${t.sparseInfillDensity}%` : "-"}</dd>
                <dt>Supports</dt><dd>{t.supports != null ? (t.supports ? "Yes" : "No") : "-"}</dd>
                {t.wallLoops != null && (<><dt>Wall loops</dt><dd>{t.wallLoops}</dd></>)}
              </dl>
            </div>

            {/* summary pills */}
            <div className="rp-summary" aria-live="polite">
              <div className="rp-pill"><strong>Used Filament</strong><span>{usedGramsText}</span></div>
              <div className="rp-pill"><strong>{timeLabel}</strong><span>{timeText}</span></div>
            </div>

            {err && <div className="rp-error">{err}</div>}
          </div>
        </div>

        {/* sticky actions */}
        <div className="rp-actions">
          <button className="rp-btn rp-btn--ghost" onClick={onClose} disabled={submitting}>Close</button>
          <button
            className={`rp-btn rp-cta${submitting ? " is-busy" : ""}`}
            onClick={submit}
            disabled={!isReady || submitting}
            aria-busy={submitting ? "true" : "false"}
            title={!isReady ? "This item is not a valid G-code or missing key" : "Print again"}
            type="button"
          >
            {submitting ? "Queuing..." : "Print again"}
          </button>
        </div>
      </div>
    </div>
  );
}
