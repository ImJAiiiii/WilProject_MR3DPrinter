// src/UserPrintHistoryModal.js
import React, { useEffect, useMemo, useState } from "react";
import "./UserPrintHistoryModal.css";
import { useAuth } from "./auth/AuthContext";
import { useApi } from "./api";

function fmtMinutes(min) {
  if (min == null) return "-";
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

const prettyName = (raw) => String(raw || "").replace(/\.(gcode|gco|gc)$/i, "");

// --- Key utils ----------------------------------------------------
function sanitizeKey(k) {
  if (!k) return k;
  // ตัด prefix ชื่อบัคเก็ตที่บางครั้งเผลอส่งมา เช่น "printer-store/..."
  if (k.startsWith("printer-store/")) k = k.substring("printer-store/".length);
  // normalize แบรนด์ให้ตรงกับที่เก็บใน MinIO
  k = k.replace("/HONTECH/", "/Hontech/").replace("/DELTA/", "/Delta/");
  // ตัด space/leading slash ที่เผลอปะปน
  return k.replace(/^\/+/, "");
}

function stripGcodeExt(key) {
  return key.replace(/\.(gcode|gco|gc)$/i, "");
}

// เดา key หลายแบบ (case-insensitive + oriented + จุด/ขีดล่าง)
function guessThumbCandidates(thumb, gcodeKey) {
  const list = [];
  if (thumb && !thumb.startsWith("/images/")) list.push(sanitizeKey(thumb)); // อย่าดัน placeholder เข้าลิสต์

  if (gcodeKey) {
    const base = stripGcodeExt(sanitizeKey(gcodeKey));
    const baseNoOriented = base.replace("_oriented", "");
    const bases = [base, baseNoOriented, `${baseNoOriented}_oriented`];
    const pats = [
      "{b}.preview.png",
      "{b}.preview.jpg",
      "{b}.preview.jpeg",
      "{b}_preview.png",
      "{b}_preview.jpg",
      "{b}_preview.jpeg",
      "{b}_thumb.png",
      "{b}_thumb.jpg",
      "{b}_thumb.jpeg",
      "{b}_oriented.preview.png",
      "{b}_oriented.preview.jpg",
      "{b}_oriented.preview.jpeg",
      "{b}_oriented_preview.png",
      "{b}_oriented_preview.jpg",
      "{b}_oriented_preview.jpeg",
    ];
    for (const b of bases) for (const p of pats) list.push(p.replace("{b}", b));
  }

  // case variants (Hontech/HONTECH/Hontec และ Delta/DELTA)
  const extra = [];
  for (const k of list) {
    if (!k) continue;
    if (k.includes("/Hontech/")) {
      extra.push(k.replace("/Hontech/", "/HONTECH/"));
      extra.push(k.replace("/Hontech/", "/Hontec/"));
    }
    if (k.includes("/HONTECH/")) extra.push(k.replace("/HONTECH/", "/Hontech/"));
    if (k.includes("/Delta/")) extra.push(k.replace("/Delta/", "/DELTA/"));
    if (k.includes("/DELTA/")) extra.push(k.replace("/DELTA/", "/Delta/"));
  }

  return Array.from(new Set([...list, ...extra]));
}

function buildPresignUrl(apiBase, key) {
  const u = new URL(`${apiBase}/api/storage/presign`);
  u.searchParams.set("object_key", sanitizeKey(key));
  return u.toString();
}

// --- Server → UI mapper -------------------------------------------
function fromServerHistoryItem(j) {
  const uploadedAt =
    j.finished_at ? new Date(j.finished_at).getTime()
    : j.uploaded_at ? new Date(j.uploaded_at).getTime()
    : Date.now();

  const gcodeKey =
    j.gcode_key || j.gcode_path || j?.file?.object_key || null;

  // เลือก thumb จากฝั่งเซิร์ฟเวอร์ก่อน (อย่าให้ fallback เป็น placeholder ที่นี่)
  const thumbServer =
    j?.file?.thumb ||
    j?.file?.preview_key ||
    j?.thumb ||
    null;

  const name = prettyName(j?.file?.filename || j?.name);
  const time_min = j.time_min ?? j?.stats?.time_min ?? null;
  const filament_g = j?.stats?.filament_g ?? null;
  const time_text =
    j?.stats?.time_text ?? (Number.isFinite(time_min) ? fmtMinutes(time_min) : "-");

  return {
    id: j.id,
    name,
    rawName: j.name,
    status: j.status,
    source: j.source,
    thumb: thumbServer,              // null ถ้าไม่มีจริง ๆ
    uploadedAt,
    gcode_key: gcodeKey,
    file: j.file,
    stats: { timeMin: time_min, time_text, filament_g },
  };
}

// --- Smart image loader (presign-first) ----------------------------
const presignCache = new Map(); // key -> presigned URL

function SmartImg({ apiBase, token, thumb, gcodeKey, alt = "", className = "" }) {
  const [idx, setIdx] = useState(0);
  const [src, setSrc] = useState("");

  const candidates = useMemo(
    () => guessThumbCandidates(thumb, gcodeKey),
    [thumb, gcodeKey]
  );

  useEffect(() => {
    let aborted = false;
    setSrc("");
    setIdx(0);
    return () => { aborted = true; };
  }, [thumb, gcodeKey]);

  useEffect(() => {
    let aborted = false;

    async function run() {
      const cand = candidates[idx];
      if (!cand) return;

      // URL ตรง → ใช้ได้เลย
      if (/^https?:\/\//i.test(cand)) {
        setSrc(cand);
        return;
      }

      const clean = sanitizeKey(cand);

      // cache
      if (presignCache.has(clean)) {
        setSrc(presignCache.get(clean));
        return;
      }

      // ขอ presign
      try {
        const url = buildPresignUrl(apiBase, clean);
        const res = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error(`presign ${res.status}`);
        // รองรับทั้ง {"url":"..."} และ body เป็นสตริง "..."
        let finalUrl = null;
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
          const data = await res.json();
          finalUrl = data?.url || null;
        } else {
          const text = await res.text();
          finalUrl = text?.replace(/^"+|"+$/g, "") || null;
        }
        if (!finalUrl) throw new Error("empty presign url");
        presignCache.set(clean, finalUrl);
        if (!aborted) setSrc(finalUrl);
      } catch (_) {
        // presign ล้มเหลว → ไปตัวถัดไป
        if (!aborted && idx < candidates.length - 1) {
          setIdx(idx + 1);
        }
      }
    }

    if (candidates.length > 0) run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idx, candidates, apiBase, token]);

  const handleError = () => {
    if (idx < candidates.length - 1) setIdx(idx + 1);
  };

  return (
    <img
      src={src || "/images/3D.png"}
      alt={alt}
      className={className}
      onError={handleError}
      draggable="false"
    />
  );
}

// --- Main component -----------------------------------------------
export default function UserPrintHistoryModal({ open, onClose }) {
  const { user, token } = useAuth();
  const api = useApi();
  const userId = user?.employee_id || user?.id || "anon";

  const [q, setQ] = useState("");
  const [days, setDays] = useState(0);
  const [selectedId, setSelectedId] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [historyList, setHistoryList] = useState([]);

  // โหลดเฉพาะจาก backend
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

        const res = await api.get("/api/history/my", params, { timeoutMs: 20000 });
        if (!alive) return;

        const list = Array.isArray(res)
          ? res
          : Array.isArray(res?.items)
          ? res.items
          : [];

        const normalized = list
          .map(fromServerHistoryItem)
          .filter((it) => ["octoprint", "upload", "storage"].includes(it.source || ""))
          .sort((a, b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));

        setHistoryList(normalized);
        setSelectedId(normalized.length > 0 ? normalized[0].id : null);
      } catch (e) {
        console.error("❌ GET /api/history/my failed:", e);
        setErr("Failed to load history from server.");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [open, userId, days, q, api]);

  const items = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const cutoffTs = days > 0 ? Date.now() - days * 864e5 : 0;
    return historyList.filter((x) => {
      const hitKw = !kw || (x.name || "").toLowerCase().includes(kw);
      const ts = x.uploadedAt ? +x.uploadedAt : 0;
      const hitDate = cutoffTs === 0 || ts >= cutoffTs;
      return hitKw && hitDate;
    });
  }, [q, days, historyList]);

  const selected = useMemo(
    () => items.find((x) => x.id === selectedId) || null,
    [items, selectedId]
  );

  const printAgain = async () => {
    if (!selected || submitting) return;
    setErr("");
    const gcode_key = selected.gcode_key;
    if (!gcode_key) {
      setErr("This item is missing its G-code key.");
      return;
    }

    try {
      setSubmitting(true);
      const payload = {
        name: selected.rawName || selected.name,
        source: "history",
        thumb: selected.thumb ?? undefined,
        gcode_key,
        original_key: selected.gcode_key,
      };
      await api.post("/api/print", payload, {
        printer_id: process.env.REACT_APP_PRINTER_ID || "prusa-core-one",
      });
      api?.toast?.success?.("Added to print queue");
      onClose?.();
    } catch (e) {
      console.error(e);
      setErr(e?.message || "Failed to reprint.");
    } finally {
      setSubmitting(false);
    }
  };

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
                        onClick={()=>setDays(btn.v)}>
                  {btn.label}
                </button>
              ))}
            </div>

            <div className="uph-search">
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search your printed files"
              />
              {q && <button className="uph-clear" onClick={()=>setQ("")}>×</button>}
            </div>
          </div>
        </div>

        <div className="uph-body">
          <div className="uph-list">
            {err && <div className="uph-empty uph-error">{err}</div>}
            {!err && loading && items.length === 0 && <div className="uph-empty">Loading your history…</div>}
            {!err && !loading && items.length === 0 && <div className="uph-empty">No print history yet.</div>}
            {!err && items.map((item) => (
              <button key={item.id}
                      className={`uph-item ${item.id === selectedId ? "is-selected" : ""}`}
                      onClick={() => setSelectedId(item.id)}>
                <SmartImg apiBase={api.API_BASE} token={token} thumb={item.thumb} gcodeKey={item.gcode_key} className="uph-thumb" />
                <div className="uph-meta">
                  <div className="uph-name">{item.name}</div>
                  <div className="uph-sub">
                    <span>{item.source}</span>
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

                <button className="uph-cta" onClick={printAgain} disabled={submitting}>
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
