// src/pages/StoragePage.js
import React, { useEffect, useMemo, useState, useCallback, useRef } from "react";
import "./StoragePage.css";
import StorageReprintModal from "../StorageReprintModal";
import { useApi } from "../api";
import { useAuth } from "../auth/AuthContext";

/* ---------- utils ---------- */
function parseTs(v) {
  try {
    if (!v && v !== 0) return 0;
    if (typeof v === "number") return v;
    const t = Date.parse(v);
    return Number.isFinite(t) ? t : 0;
  } catch { return 0; }
}
function fmtLocal(ts) { try { return ts ? new Date(ts).toLocaleString() : ""; } catch { return ""; } }
function getExt(name = "") { const i = name.lastIndexOf("."); return i >= 0 ? name.slice(i + 1).toLowerCase() : ""; }
function isGcodeExt(ext) { const e = (ext || "").toLowerCase(); return e === "gcode" || e === "gco" || e === "gc"; }
function useDebounce(value, delay = 300) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), delay); return () => clearTimeout(t); }, [value, delay]);
  return v;
}
function looksLikeFilename(s = "") {
  const x = (s || "").trim().toLowerCase();
  if (!x) return false;
  if (x.includes("/") || x.includes("\\")) return true;
  if (x.includes(".")) {
    const ext = x.split(".").pop();
    return ["gcode","gco","gc","stl","3mf","obj","png","jpg","jpeg","webp","svg","gif","pdf"].includes(ext);
  }
  return false;
}

/* ✅ Filters (ยังคงเหมือนเดิม) */
const FILTERS = ["ALL", "DELTA", "HONTECH", "OTHER"];

/* ---------- preview helpers (PNG from MinIO) ---------- */
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
function toRawUrl(apiBase, objectKey, token) {
  const path = `/api/files/raw?object_key=${encodeURIComponent(objectKey)}`;
  return withToken(joinUrl(apiBase, path), token);
}
function derivePreviewCandidatesFromGcodeKey(k) {
  if (!k) return [];
  const i = k.lastIndexOf(".");
  const base = i >= 0 ? k.slice(0, i) : k;
  const a = `${base}.preview.png`;
  const b = `${base}_preview.png`;
  return a === b ? [a] : [a, b];
}
function buildPreviewPair({ apiBase, token, key, cacheTag }) {
  if (!key) return { src: "", alt: "" };
  const [c1, c2] = derivePreviewCandidatesFromGcodeKey(key);
  const t = cacheTag || Date.now();
  const mk = (k) => (k ? `${toRawUrl(apiBase, k, token)}&t=${encodeURIComponent(t)}` : "");
  return { src: mk(c1), alt: mk(c2) };
}
function makeOnImgError(fallbackSrc) {
  return (e) => {
    const el = e.currentTarget;
    const alt = el.dataset.altSrc;
    if (alt) {
      el.onerror = null;
      el.removeAttribute("data-alt-src");
      el.src = alt;
      return;
    }
    el.onerror = null;
    el.src = fallbackSrc;
  };
}

/* ---------- component ---------- */
export default function StoragePage({ items = [], onQueue, onDeleteItem }) {
  const api = useApi();
  const { user, token } = useAuth();

  // ใช้ null เป็น state ขณะโหลด
  const [serverItems, setServerItems] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounce(q, 300);

  // filter model
  const [modelFilter, setModelFilter] = useState(() => {
    try { return localStorage.getItem("storage:modelFilter") || "ALL"; } catch { return "ALL"; }
  });
  useEffect(() => { try { localStorage.setItem("storage:modelFilter", modelFilter); } catch {} }, [modelFilter]);

  const [selectedFile, setSelectedFile] = useState(null);
  const [openReprint, setOpenReprint] = useState(false);

  // กันดับเบิลลบ
  const [deletingKey, setDeletingKey] = useState(null);

  // prevent race
  const fetchSeq = useRef(0);

  /* ---------- fetch from backend (catalog) ---------- */
  const fetchServer = useCallback(async () => {
    const mySeq = ++fetchSeq.current;
    setErr("");
    setLoading(true);
    setServerItems(null);
    try {
      const toApiModel = (x) =>
        x === "DELTA"   ? "Delta"   :
        x === "HONTECH" ? "Hontech" :
        x === "OTHER"   ? "Other"   : undefined;
      const modelParam = modelFilter === "ALL" ? undefined : toApiModel(modelFilter);

      const res = await api.get(
        "/api/storage/catalog",
        {
          model: modelParam,
          q: dq || undefined,
          offset: 0,
          limit: 2000,
          with_urls: 1,
          with_head: 1,
        },
        { timeoutMs: 20000 }
      );
      const list = (res && Array.isArray(res.items)) ? res.items : [];
      if (mySeq === fetchSeq.current) setServerItems(list);
    } catch (e) {
      if (mySeq === fetchSeq.current) {
        console.error("storage.catalog failed:", e);
        setErr(e?.message || "Failed to load storage.");
        setServerItems([]);
      }
    } finally {
      if (mySeq === fetchSeq.current) setLoading(false);
    }
  }, [api, modelFilter, dq]);

  useEffect(() => { fetchServer(); }, [fetchServer]);
  useEffect(() => { setErr(""); }, [dq, modelFilter]);

  /* ---------- adaptors ---------- */
  const adaptFromServer = useCallback((raw) => {
    const name = raw?.display_name || raw?.name || raw?.filename || "";
    // ⛑️ เดา ext จาก object_key เป็น fallback กันกรองตกหล่น
    const ext = (raw?.ext) || getExt(raw?.filename || name) || getExt(raw?.object_key || "");
    const isGcode = isGcodeExt(ext);
    const ts = parseTs(raw?.uploaded_at);

    // preview url
    let thumbUrl = null, thumbAlt = "";
    if (typeof raw?.preview_url === "string" && /^https?:\/\//i.test(raw.preview_url)) {
      thumbUrl = raw.preview_url;
    } else if (typeof raw?.preview_url === "string" && raw.preview_url) {
      if (raw.preview_url.startsWith("/images/")) {
        thumbUrl = raw.preview_url;
      } else {
        thumbUrl = toRawUrl(api.API_BASE, raw.preview_url, token);
      }
    }

    if (!thumbUrl) {
      if (raw?.thumb) {
        if (raw.thumb.startsWith("/images/")) {
          thumbUrl = raw.thumb;
        } else {
          thumbUrl = toRawUrl(api.API_BASE, raw.thumb, token);
        }
      } else {
        const gk = raw?.gcode_key || (isGcode ? raw?.object_key : null);
        const { src, alt } = buildPreviewPair({
          apiBase: api.API_BASE,
          token,
          key: gk || raw?.object_key,
          cacheTag: raw?.updated_at || raw?.uploaded_at || raw?.mtime
        });
        thumbUrl = src || process.env.PUBLIC_URL + "/images/3D.png";
        thumbAlt = alt || "";
      }
    }

    const up = raw?.uploader || null;
    let uploaderName = (up?.name || up?.employee_id || "") || null;
    if (uploaderName && (uploaderName.toLowerCase() === (name || "").toLowerCase() || looksLikeFilename(uploaderName))) {
      uploaderName = null;
    }
    const uploaderEmp = up?.employee_id || null;

    let sizeText = raw?.size_text || null;
    if (!sizeText && typeof raw?.size === "number") {
      const mb = raw.size / (1024 * 1024);
      sizeText = `${mb >= 1 ? mb.toFixed(1) : Math.max(raw.size, 1)} ${mb >= 1 ? "MB" : "B"}`;
    }

    return {
      id: raw?.object_key || name,
      name,
      piece: null,
      model: raw?.model || "",
      ext,
      isGcode,
      uploadedTs: ts,
      uploadedAt: fmtLocal(ts),
      sizeText,
      thumb: thumbUrl || "/icon/file.png",
      thumbAlt,
      _raw: raw,
      storageId: raw?.id ?? null,
      object_key: raw?.object_key || null,
      gcode_key: raw?.gcode_key || (isGcode ? raw?.object_key : null),
      preview_key: raw?.thumb || null,
      template: null,
      stats: raw?.stats ?? null,
      uploader: uploaderName,
      uploaderEmployeeId: uploaderEmp
    };
  }, [api, token]);

  /* ---------- merge ---------- */
  // ❗ ใช้เฉพาะรายการที่มาจาก server (catalog) เท่านั้น
  const merged = useMemo(() => {
    return (serverItems || []).map(adaptFromServer);
  }, [serverItems, adaptFromServer]);

  /* ---------- client-side search ---------- */
  const files = useMemo(() => {
    const kw = q.trim().toLowerCase();
    if (!kw || kw === (dq || "").trim().toLowerCase()) return merged;
    return merged.filter(f =>
      (f.name || "").toLowerCase().includes(kw) ||
      ((f.uploader || "")).toLowerCase().includes(kw) ||
      (f.model || "").toLowerCase().includes(kw)
    );
  }, [q, dq, merged]);

  /* ---------- show only G-code (and only catalog/*) ---------- */
  const gcodeFiles = useMemo(() => {
    const isCtGcode = (ct = "") =>
      /(?:^|\/)(?:text|application)\/x?-?gcode$/i.test(String(ct).trim());
    const hasValidKey = (k = "") => /^(catalog)\//.test(String(k));
    return (files || []).filter((f) => {
      if (!hasValidKey(f?.object_key)) return false;
      if (f?.isGcode) return true;
      const ct = f?._raw?.content_type || f?._raw?.contentType || "";
      return isCtGcode(ct);
    });
  }, [files]);

  /* ---------- permissions ---------- */
  const isManager = !!(user?.is_manager || user?.can_manage_queue || (user?.role || "").toLowerCase() === "manager");
  const isOwner = useCallback((f) => {
    if (!user) return false;
    const emp = (user.employee_id || "").trim();
    const up = (f?.uploaderEmployeeId || "").trim();
    return emp && up && emp === up;
  }, [user]);

  const canDelete = useCallback(
    (f) => f?.isGcode && (isOwner(f) || isManager) && (f.storageId != null || !!f.object_key),
    [isOwner, isManager]
  );

  /* ---------- actions ---------- */
  const openModal = (f) => { setSelectedFile(f); setOpenReprint(true); };

  const handlePrintAgain = async () => {
    if (!selectedFile) return;
    try { onQueue?.(selectedFile); } finally {
      setOpenReprint(false);
      setSelectedFile(null);
      fetchServer();
    }
  };

  const removeFromList = useCallback((objKeyOrId) => {
    setServerItems((prev) => {
      if (!Array.isArray(prev)) return prev;
      return prev.filter((r) => (r?.object_key || r?.id) !== objKeyOrId);
    });
  }, []);

  const handleDelete = async (e, f) => {
    e.stopPropagation();
    if (!canDelete(f) || deletingKey) return;

    const pretty = f.name || f.object_key || "this file";
    const ok = window.confirm(
      `Delete EVERYTHING of "${pretty}"?\n\nThis will remove the G-code, preview image(s), manifest and database records.`
    );
    if (!ok) return;

    const keyForState = f.object_key || f.id || f.name;

    setDeletingKey(keyForState);
    removeFromList(f.object_key);

    try {
      await api.del(
        "/api/storage/object-hard",
        { object_key: f.object_key },
        { timeoutMs: 18000 }
      );
      await fetchServer();
    } catch (error) {
      console.error("hard delete failed:", error);
      const msg = String(error?.message || error || "Delete failed");
      if (/409|file_in_use_by_active_jobs/i.test(msg)) {
        alert("This file is currently used by an active print job.\nPlease cancel/finish that job, then try again.");
      } else if (/403/.test(msg)) {
        alert("You don't have permission to delete this file.");
      } else {
        alert(msg);
      }

      // Fallback compatibility (เผื่อรุ่นเก่า)
      try {
        if (f.storageId != null && api.storage?.deleteById) {
          await api.storage.deleteById(
            f.storageId,
            { delete_object_from_s3: true },
            { timeoutMs: 15000 }
          );
        } else if (f.object_key) {
          if (api.storage?.deleteByKey) {
            await api.storage.deleteByKey(
              { object_key: f.object_key, delete_object_from_s3: true },
              { timeoutMs: 15000 }
            );
          } else {
            await api.del(
              "/api/storage/by-key",
              { object_key: f.object_key, delete_object_from_s3: true },
              { timeoutMs: 15000 }
            );
          }
        }
      } catch (fallbackErr) {
        console.warn("fallback delete also failed:", fallbackErr);
        if (onDeleteItem) onDeleteItem(f.id);
      } finally {
        await fetchServer();
      }
    } finally {
      setDeletingKey(null);
    }
  };

  const placeholder = process.env.PUBLIC_URL + "/images/placeholder-model.png";
  const fallbackImg = process.env.PUBLIC_URL + "/images/3D.png";
  const onImgError = makeOnImgError(fallbackImg);

  return (
    <div className="storage-page">
      {/* Header: left = Search + Filters, right = counter + refresh */}
      <div className="storage-header" style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, flex: 1, minWidth: 240 }}>
          <div className="storage-search">
            <img src={process.env.PUBLIC_URL + "/icon/search.png"} alt="search" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Escape" && q) setQ(""); }}
              placeholder="Search pieces/files"
              aria-label="Search files"
              style={{ height: 38 }}
            />
          </div>

          <div className="storage-filters" role="tablist" aria-label="Model filter">
            {FILTERS.map(m => (
              <button
                key={m}
                role="tab"
                aria-selected={modelFilter === m}
                className={`segmented-btn ${modelFilter === m ? "active" : ""}`}
                onClick={() => setModelFilter(m)}
              >
                {m === "ALL" ? "All"
                  : m === "DELTA" ? "Delta"
                  : m === "HONTECH" ? "Hontech"
                  : "Other"}
              </button>
            ))}
          </div>
        </div>

        <div className="file-count" aria-live="polite" style={{ paddingTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
          {loading ? "…" : `${gcodeFiles.length} item${gcodeFiles.length === 1 ? "" : "s"}`}
          <button
            type="button"
            className="btn-refresh"
            onClick={fetchServer}
            title="Refresh"
            aria-label="Refresh"
            disabled={loading}
          >
            ↻
          </button>
        </div>
      </div>

      {!!err && <div className="storage-error" role="alert">{err}</div>}

      {(!loading && gcodeFiles.length === 0) ? (
        <div style={{ textAlign: "center", color: "#667", padding: "48px 12px" }}>
          <img
            src={process.env.PUBLIC_URL + "/icon/file.png"}
            alt=""
            width="72"
            height="72"
            style={{ opacity: .7 }}
            onError={(e) => { e.currentTarget.style.display = "none"; }}
          />
          <div style={{ marginTop: 8, fontWeight: 600 }}>No items</div>
          <div style={{ fontSize: 12 }}>There are no items in catalog storage.</div>
        </div>
      ) : (
        <div className="file-grid" aria-busy={loading ? "true" : "false"}>
          {gcodeFiles.map((f) => {
            const delDisabled = deletingKey && (deletingKey === (f.object_key || f.id || f.name));
            return (
              <div key={`${f.object_key || f.id || f.name}`} className="file-card-wrap">
                <button
                  className="file-card"
                  onClick={() => openModal(f)}
                  onKeyDown={(e) => (e.key === "Enter") && openModal(f)}
                  aria-label={`Open ${f.name}${f.uploader ? `, uploaded by ${f.uploader}` : ""}`}
                  title={f.name}
                >
                  <div className="thumb-wrap">
                    <img
                      className="file-thumb"
                      src={f.thumb || placeholder}
                      data-alt-src={f.thumbAlt || ""}
                      alt={f.name}
                      onError={onImgError}
                      loading="lazy"
                      decoding="async"
                      draggable="false"
                    />
                  </div>

                  <div className="file-name">
                    <div className="file-title" title={f.name}>
                      {f.name}
                    </div>

                    <div className="file-uploader">
                      {f.uploader ? <span>by {f.uploader}</span> : null}
                    </div>
                  </div>

                  <div className="file-meta">
                    {!!f.sizeText && <span className="meta">{f.sizeText}</span>}
                    <span className="meta">{f.isGcode ? "G-code" : (f.ext || "").toUpperCase() || "—"}</span>
                    {!!f.model && <span className="meta">{f.model}</span>}
                  </div>
                </button>

                {canDelete(f) && (
                  <div className="file-actions-row">
                    <button
                      className="file-delete"
                      title={delDisabled ? "Deleting…" : "Delete (owner/manager only)"}
                      aria-label={`Delete ${f.name}`}
                      onClick={(e) => handleDelete(e, f)}
                      disabled={!!delDisabled}
                    >
                      {delDisabled ? "…" : "×"}
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <StorageReprintModal
        open={openReprint}
        file={selectedFile}
        onClose={() => { setOpenReprint(false); setSelectedFile(null); }}
        onPrint={handlePrintAgain}
      />
    </div>
  );
}