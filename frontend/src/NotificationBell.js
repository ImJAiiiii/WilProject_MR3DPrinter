// src/NotificationBell.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "./auth/AuthContext";
import { useApi } from "./api";
import "./NotificationBell.css";

export default function NotificationBell({
  eventsUrl = "/api/notifications/stream",
  pollIntervalMs = 10000,
  onOpenPrinting,
  printerId = null, // legacy
  size = 42,
  iconScale = 0.78,
  dotScale = 0.22,
  nameSizePx = 17,
}) {
  const { token, user } = useAuth() || {};
  const api = useApi();

  const SUPPRESS_STATUSES = new Set(["started"]);

  // ---------- cache per-user ----------
  const CACHE_KEY = useMemo(
    () => `notif.items::${user?.id ?? "anon"}`,
    [user?.id]
  );
  const writeCache = (list) => {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(list.slice(0, 100))); } catch {}
  };
  const readCache = () => {
    try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "[]"); } catch { return []; }
  };

  const [open, setOpen] = useState(false);
  const [items, setItems] = useState(() => readCache());
  const [hasUnread, setHasUnread] = useState(() => items.some((i) => !i.read));

  const btnRef = useRef(null);
  const panelRef = useRef(null);
  const sseRef = useRef(null);
  const pollRef = useRef(null);
  const pauseUntilRef = useRef(0);

  useEffect(() => {
    const cached = readCache();
    setItems(cached);
    setHasUnread(cached.some((i) => !i.read));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [CACHE_KEY]);

  // CSS vars
  const styleVars = useMemo(
    () => ({
      "--nb-size": `${size}px`,
      "--nb-icon": `${Math.round(size * iconScale)}px`,
      "--nb-dot": `${Math.max(8, Math.round(size * dotScale))}px`,
      "--nb-name-size": `${nameSizePx}px`,
    }),
    [size, iconScale, dotScale, nameSizePx]
  );

  // Icons
  const iconBell = useMemo(() => process.env.PUBLIC_URL + "/icon/notification.png", []);
  const iconBellActive = process.env.PUBLIC_URL + "/icon/bluenotification.png";

  // ---------- helpers ----------
  const NAME_MAX_CHARS = 34;
  function centerEllipsis(str, max = NAME_MAX_CHARS) {
    if (!str) return "";
    if (str.length <= max) return str;
    const keep = Math.max(4, Math.floor((max - 1) / 2));
    return `${str.slice(0, keep)}…${str.slice(-keep)}`;
  }

  // show absolute clock, e.g. "09.00"
  function clockText(ts) {
    const d = new Date(ts || Date.now());
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}.${mm}`;
  }

  // strip leading quoted job name from a message
  function stripNamePrefix(s) {
    if (!s) return s;
    // remove “...”, "..." or '...' at the start + trailing space
    return s.replace(/^[“"'’`][^“"'’`]+[”"'’`]\s*/u, "");
  }

  function normStatusFromType(t) {
    const s = String(t || "").toLowerCase();
    if (s.includes("print.queued")) return "queued";
    if (s.includes("print.started")) return "started";
    if (s.includes("print.processing")) return "processing";
    if (s.includes("print.completed")) return "completed";
    if (s.includes("print.failed")) return "failed";
    if (s.includes("print.canceled") || s.includes("print.cancelled")) return "canceled";
    if (s.includes("print.paused")) return "paused";
    return "issue";
  }

  // ---------- Turn raw payload → label/message ----------
  function describeEventCore(p) {
    const t = String(p.type || p.ntype || "").toLowerCase();
    const sev = String(p.severity || "info").toLowerCase();
    const data = p.data || {};

    const name =
      data.name ?? data.job_name ?? data.job ?? data.model ?? p.name ?? p.jobName ?? null;

    const prn =
      data.printer_id ?? p.printer_id ?? data.printerId ?? p.printerId ?? "printer";

    const is = (kw) => t.includes(kw);
    let label = "Notification";
    let kind = "info";
    let message = stripNamePrefix((p.message || "").trim()); // 👈 ตัดชื่อออก
    let status = normStatusFromType(t);

    const msg = {
      queued: `Queued on ${prn}.`,
      starting: `Starting on ${prn}.`,
      printing: `Printing on ${prn}.`,
      completed: `Finished on ${prn}.`,
      failed: `Failed on ${prn}.`,
      canceled: `Canceled on ${prn}.`,
      paused: `Paused on ${prn}.`,
    };

    if (is("print.started") || is("print.processing")) {
      label = is("print.processing") ? "Printing" : "Starting";
      kind = "info";
      status = is("print.processing") ? "processing" : "started";
      if (!message) message = status === "processing" ? msg.printing : msg.starting;
    } else if (is("print.completed")) {
      label = "Print completed"; kind = "success"; status = "completed";
      if (!message) message = msg.completed;
    } else if (is("print.failed")) {
      label = "Print failed"; kind = "error"; status = "failed";
      if (!message) message = msg.failed;
    } else if (is("print.cancelled") || is("print.canceled")) {
      label = "Print canceled"; kind = "neutral"; status = "canceled";
      if (!message) message = msg.canceled;
    } else if (is("print.paused")) {
      label = "Paused"; kind = "warning"; status = "paused";
      if (!message) message = msg.paused;
    } else if (is("print_issue") || sev === "critical" || sev === "warning") {
      label = p.title || (sev === "critical" ? "Alert (critical)" : "Alert");
      kind = sev === "critical" || sev === "error" ? "error" : sev === "warning" ? "warning" : "info";
      status = "issue";
      if (!message) {
        const cls = data.detected_class || p.detected_class || "-";
        const conf = data.confidence ?? p.confidence;
        const confTxt = conf != null ? ` (${Number(conf).toFixed(2)})` : "";
        message = `Anomaly detected: ${cls}${confTxt} • ${prn}`;
      }
    } else {
      label = p.title || "Notification";
      kind = sev === "error" || /fail|error/.test(t) ? "error" : "info";
      status = kind === "error" ? "issue" : "queued";
      if (!message) message = kind === "error" ? `Error on ${prn}.` : `Updated on ${prn}.`;
    }

    return { tagTextEn: label, kind, status, prettyMessage: message, name, printerText: prn };
  }

  // ---------- Normalize ----------
  function normalize(input, namedType) {
    let payload = input;
    try { if (typeof input === "string") payload = JSON.parse(input); } catch {}
    const p = payload || {};
    if (namedType && !p.type) p.type = namedType;

    const fallbackId = `${Date.parse(p.created_at || "") || Date.now()}-${(p.type || "")}-${(p.message || "").slice(0, 64)}`;
    const id = p.id ?? fallbackId;

    const desc = describeEventCore(p);
    return {
      id,
      kind: desc.kind,
      status: desc.status,
      tagTextEn: desc.tagTextEn,
      name: desc.name,
      message: desc.prettyMessage,
      printerId: desc.printerText,
      // 🕒 ไม่ใช้ created_at อีกต่อไป เราจะ override เป็นเวลาที่รับจริงด้านล่าง
      time: Date.now(),
      read: !!p.read,
    };
  }

  function safeAppend(data, namedType, opts = { silent: false }) {
    // สร้างจาก payload แล้ว “บังคับเวลาเป็นตอนนี้”
    const now = Date.now();
    const item = { ...normalize(data, namedType), time: now };

    // drop unwanted cards
    if (SUPPRESS_STATUSES.has(item.status)) return;

    setItems((prev) => {
      // Duplicate detection ใช้เวลา now เช่นกัน
      if (prev.some((x) => String(x.id) === String(item.id))) return prev;

      const nearDup = prev.find(
        (x) =>
          x.status === item.status &&
          x.name === item.name &&
          x.message === item.message &&
          Math.abs(x.time - item.time) < 3000
      );
      if (nearDup) return prev;

      const next = [item, ...prev].slice(0, 100);
      writeCache(next);
      return next;
    });
    if (!item.read) setHasUnread(true);
    if (!opts.silent) beep(item.kind === "error" ? 600 : 880);
  }

  // ---------- SSE subscribe ----------
  const sseUrl = useMemo(() => {
    if (!eventsUrl) return null;
    const base = api?.API_BASE || (typeof window !== "undefined" ? window.location.origin : "http://localhost");
    const u = new URL(eventsUrl, base);
    if (token) u.searchParams.set("token", token);
    if (!u.searchParams.has("init_limit")) u.searchParams.set("init_limit", "20");
    return u.toString();
  }, [api?.API_BASE, eventsUrl, token]);

  useEffect(() => {
    if (!token || !sseUrl) { startPolling(pollIntervalMs); return; }
    let es = null, stopped = false, backoff = 1000;

    const start = () => {
      if (stopped) return;
      try { es = new EventSource(sseUrl); } catch { es = null; }
      if (!es) { startPolling(Math.max(pollIntervalMs, 5000)); return; }
      sseRef.current = es;

      es.onopen = () => { backoff = 1000; stopPolling(); };
      es.onmessage = (e) => safeAppend(e.data, undefined, { silent: false });
      es.addEventListener("backlog", (e) => safeAppend(e.data, undefined, { silent: true }));

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
  }, [sseUrl, token, pollIntervalMs]);

  // ---------- Polling fallback ----------
  function startPolling(intervalMs) {
    stopPolling();
    pollRef.current = setInterval(async () => {
      if (!token) return;
      const now = Date.now();
      if (now < pauseUntilRef.current) return;
      if (document.visibilityState === "hidden") return;

      try {
        const arr = await api.notifications.list({ limit: 20 }, { timeout: 12000, retries: 1 });
        if (Array.isArray(arr)) arr.forEach((evt) => safeAppend(evt, undefined, { silent: true }));
      } catch {
        pauseUntilRef.current = Date.now() + 15000;
      }
    }, Math.max(3000, intervalMs | 0));
  }
  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  // ---------- close behaviors ----------
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
  async function markAllRead() {
    try { await api.notifications.markAllRead(); } catch {}
    setItems((prev) => {
      const next = prev.map((x) => ({ ...x, read: true }));
      writeCache(next);
      return next;
    });
    setHasUnread(false);
  }

  async function removeOne(id, evt) {
    if (evt?.stopPropagation) evt.stopPropagation();
    setItems((prev) => {
      const next = prev.filter((x) => String(x.id) !== String(id));
      writeCache(next);
      return next;
    });
    setHasUnread(() => {
      try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "[]").some((x) => !x.read); }
      catch { return false; }
    });
    try { await api.notifications.remove(id); } catch {}
  }

  async function handleClickItem(id) {
    onOpenPrinting?.();
    setItems((prev) => {
      const next = prev.map((x) => (String(x.id) === String(id) ? { ...x, read: true } : x));
      writeCache(next);
      return next;
    });
    setHasUnread(() => {
      try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "[]").some((x) => !x.read); }
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
        onClick={() => setOpen((v) => !v)}
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

            {items.map((it) => {
              const shownName = centerEllipsis(it.name || "");
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
                        <span className={`badge ${it.kind}`}>{it.tagTextEn}</span>
                        {it.name ? (
                          <span className="name" title={it.name} dir="auto" aria-label={it.name}>
                            {shownName}
                          </span>
                        ) : null}
                        <time className="time">{clockText(it.time)}</time>
                      </div>
                      <div className="line2">{it.message}</div>
                      <div className="line3">
                        on <strong>{it.printerId}</strong>
                      </div>
                    </div>
                  </button>

                  <button className="notif-remove" aria-label="Delete notification" title="Delete" onClick={(e) => removeOne(it.id, e)}>
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

function beep(freq = 880) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = freq; g.gain.value = 0.01;
    o.start(); setTimeout(() => { o.stop(); ctx.close(); }, 120);
  } catch {}
}
