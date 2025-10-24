// src/NotificationBell.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "./auth/AuthContext";
import { useApi } from "./api";
import "./NotificationBell.css";

/**
 * NotificationBell
 * - SSE    : GET  /api/notifications/stream?token=...&printerId=...
 * - Polling: api.notifications.list({limit})
 * - Mark   : api.notifications.markAllRead()
 * - Remove : api.notifications.remove(id)
 */
export default function NotificationBell({
  eventsUrl = "/api/notifications/stream",
  pollIntervalMs = 10000,
  onOpenPrinting,
  printerId = null,
  size = 42,
  iconScale = 0.78,
  dotScale = 0.22,
}) {
  const { token, user } = useAuth() || {};
  const api = useApi();

  // ---------- cache per-user ----------
  const CACHE_KEY = useMemo(() => `notif.items::${user?.id ?? "anon"}`, [user?.id]);
  const writeCache = (list) => {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(list.slice(0, 100))); } catch {}
  };
  const readCache  = () => {
    try { const raw = localStorage.getItem(CACHE_KEY); return raw ? JSON.parse(raw) : []; }
    catch { return []; }
  };

  const [open, setOpen] = useState(false);
  const [items, setItems] = useState(() => readCache());
  const [hasUnread, setHasUnread] = useState(() => items.some(i => !i.read));

  const btnRef = useRef(null);
  const panelRef = useRef(null);
  const sseRef = useRef(null);
  const pollRef = useRef(null);
  const pauseUntilRef = useRef(0);

  // ใช้ CSS variables คุมสเกลทั้งหมด
  const styleVars = useMemo(() => ({
    '--nb-size': `${size}px`,
    '--nb-icon': `${Math.round(size * iconScale)}px`,
    '--nb-dot' : `${Math.max(8, Math.round(size * dotScale))}px`,
  }), [size, iconScale, dotScale]);

  // ไอคอน
  const iconBell       = useMemo(() => process.env.PUBLIC_URL + "/icon/notification.png", []);
  const iconBellActive = process.env.PUBLIC_URL + "/icon/bluenotification.png";

  // ---------- Normalize event payload ----------
  function normalize(e, namedType){
    let payload = e;
    try { if (typeof e === "string") payload = JSON.parse(e); } catch {}
    const id   = payload?.id ?? `${Date.now()}-${Math.random().toString(36).slice(2,8)}`;
    const type = (namedType || payload?.type || payload?.ntype || "print.completed") + "";
    const severity = (payload?.severity || "info").toLowerCase();
    const kind = (severity === "error" || /fail|error/i.test(type)) ? "error" : "done";

    const name =
      payload?.data?.job ||
      payload?.data?.model ||
      payload?.name ||
      payload?.jobName ||
      "—";

    const printer =
      payload?.data?.printerId ||
      payload?.printerId ||
      payload?.printer ||
      "Printer";

    const message =
      payload?.message ||
      (kind === "error"
        ? `Job "${name}" failed on ${printer}`
        : `Job "${name}" completed on ${printer}`);

    const createdAt = payload?.created_at ? Date.parse(payload.created_at) : Date.now();
    const read = !!payload?.read;

    return { id, kind, name, message, printerId: printer, time: createdAt, read };
  }

  function safeAppend(data, namedType){
    const item = normalize(data, namedType);
    setItems(prev => {
      if (prev.some(x => String(x.id) === String(item.id))) return prev;
      const nearDup = prev.find(x => x.message === item.message && Math.abs(x.time - item.time) < 1500);
      if (nearDup) return prev;
      const next = [item, ...prev].slice(0, 100);
      writeCache(next);
      return next;
    });
    if (!item.read) setHasUnread(true);
    beep(item.kind === "error" ? 600 : 880);
  }

  // ---------- SSE subscribe (with backoff) ----------
  const sseUrl = useMemo(() => {
    if (!eventsUrl) return null;
    const base =
      api?.API_BASE ||
      (typeof window !== "undefined" ? window.location.origin : "http://localhost");
    const u = new URL(eventsUrl, base);
    if (token) u.searchParams.set("token", token);
    if (printerId) u.searchParams.set("printerId", printerId);
    return u.toString();
  }, [api?.API_BASE, eventsUrl, token, printerId]);

  useEffect(() => {
    if (!token || !sseUrl) { startPolling(pollIntervalMs); return; }
    let es = null, stopped = false, backoff = 1000;

    const start = () => {
      if (stopped) return;
      try { es = new EventSource(sseUrl); } catch { es = null; }
      if (!es) { startPolling(Math.max(pollIntervalMs, 5000)); return; }
      sseRef.current = es;

      es.onopen = () => { backoff = 1000; stopPolling(); };
      es.onmessage = (e) => safeAppend(e.data);
      es.addEventListener("print.completed", (e) => safeAppend(e.data, "print.completed"));
      es.addEventListener("print.failed",    (e) => safeAppend(e.data, "print.failed"));
      es.onerror = () => {
        try { es.close(); } catch {}
        sseRef.current = null;
        if (!stopped) {
          setTimeout(start, backoff);
          backoff = Math.min(backoff * 2, 30000);
          if (!pollRef.current) startPolling(Math.max(pollIntervalMs, 5000));
        }
      };
    };

    start();
    return () => {
      stopped = true;
      try { es?.close(); } catch {}
      sseRef.current = null;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sseUrl, token, printerId, pollIntervalMs]);

  // ---------- Polling fallback ----------
  function startPolling(intervalMs){
    stopPolling();
    pollRef.current = setInterval(async () => {
      if (!token) return;
      const now = Date.now();
      if (now < pauseUntilRef.current) return;
      if (document.visibilityState === "hidden") return;

      try {
        const arr = await api.notifications.list({ limit: 20 }, { timeout: 12000, retries: 1 });
        if (Array.isArray(arr)) arr.forEach(evt => safeAppend(evt));
      } catch {
        pauseUntilRef.current = Date.now() + 15000;
      }
    }, Math.max(3000, intervalMs|0));
  }
  function stopPolling(){ if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } }

  // ปิด dropdown เมื่อคลิกนอก/กด ESC
  useEffect(() => {
    const onDocClick = (e) => {
      if (open && !panelRef.current?.contains(e.target) && !btnRef.current?.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    window.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDocClick); window.removeEventListener("keydown", onKey); };
  }, [open]);

  // ---------- mark / remove ----------
  async function markAllRead(){
    try { await api.notifications.markAllRead(); } catch {}
    setItems(prev => { const next = prev.map(x => ({ ...x, read: true })); writeCache(next); return next; });
    setHasUnread(false);
  }

  // ลบทันทีแบบ optimistic + กัน event ซ้อน
  async function removeOne(id, evt){
    if (evt?.stopPropagation) evt.stopPropagation(); // กันไปทริกเกอร์ปุ่มหลัก
    // ตัดออกจาก state ทันที
    setItems(prev => {
      const next = prev.filter(x => String(x.id) !== String(id));
      writeCache(next);
      return next;
    });
    setHasUnread(() => {
      try { const arr = JSON.parse(localStorage.getItem(CACHE_KEY)||"[]"); return arr.some(x => !x.read); }
      catch { return false; }
    });
    // ค่อยยิงลบที่ BE (ถ้าพลาดจะไม่ใส่กลับเพื่อความเรียบง่าย)
    try { await api.notifications.remove(id); } catch {}
  }

  async function handleClickItem(id){
    onOpenPrinting?.();
    setItems(prev => {
      const next = prev.map(x => String(x.id) === String(id) ? ({ ...x, read: true }) : x);
      writeCache(next);
      return next;
    });
    setHasUnread(() => {
      try { const arr = JSON.parse(localStorage.getItem(CACHE_KEY)||"[]"); return arr.some(x => !x.read); }
      catch { return false; }
    });
    setOpen(false);
  }

  // ---------- UI ----------
  return (
    <div className="notif-wrap" style={styleVars}>
      <button
        ref={btnRef}
        className="notif-btn"
        aria-label="Notifications"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        title="Notifications"
      >
        <img className="notif-icon" src={open ? iconBellActive : iconBell} alt="" draggable="false" />
        {hasUnread && <span className="notif-dot" aria-hidden />}
      </button>

      {open && (
        <div className="notif-panel" role="menu" ref={panelRef}>
          <div className="notif-head">
            <span>Notifications</span>
            <button className="notif-mark" onClick={markAllRead}>Mark all read</button>
          </div>

          <div className="notif-list">
            {items.length === 0 && <div className="notif-empty">No notifications yet</div>}

            {items.map(it => {
              const tagText = it.kind === "error" ? "Error" : "Completed";
              return (
                <div key={it.id} className={`notif-item ${it.read ? "" : "unread"}`} role="group">
                  <button
                    className="notif-main only-text"
                    onClick={() => handleClickItem(it.id)}
                    role="menuitem"
                    title={it.message}
                  >
                    <div className="notif-body">
                      <div className="line1">
                        <span className={`badge ${it.kind}`}>{tagText}</span>
                        <span className="name" title={it.name}>{it.name}</span>
                        <time className="time">{timeAgo(it.time)}</time>
                      </div>
                      <div className="line2">{it.message}</div>
                      <div className="line3">on <strong>{it.printerId}</strong></div>
                    </div>
                  </button>

                  <button
                    className="notif-remove"
                    aria-label="Delete notification"
                    title="Delete"
                    onClick={(e) => removeOne(it.id, e)}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function timeAgo(ts){
  const d = Math.max(0, (Date.now() - ts) / 1000);
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d/60)}m ago`;
  if (d < 86400) return `${Math.floor(d/3600)}h ago`;
  return `${Math.floor(d/86400)}d ago`;
}

function beep(freq = 880){
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = freq;
    g.gain.value = 0.01;
    o.start(); setTimeout(() => { o.stop(); ctx.close(); }, 120);
  } catch {}
}
