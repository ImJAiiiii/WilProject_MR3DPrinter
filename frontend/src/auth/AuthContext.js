// src/auth/AuthContext.js
import React, {
  createContext, useContext, useEffect, useMemo, useRef, useState,
} from "react";

/* =========================================================================
   Auth Context (long-term, refresh ready)
   - เก็บทั้ง access_token และ refresh_token
   - auto-refresh ก่อนหมดอายุ + รีเฟรชเมื่อเจอ 401 (ลอง 1 ครั้ง)
   - รองรับรูปแบบเก่า (token) โดย map -> access_token อัตโนมัติ
   - ปิด WS/ping/timers ให้เรียบร้อยตอน logout
   ========================================================================= */

const AuthCtx = createContext(null);
export const useAuth = () => useContext(AuthCtx);

/* ---------- storage keys (ใหม่ + compat ของเก่า) ---------- */
const LS_ACCESS = "auth.access_token";
const LS_REFRESH = "auth.refresh_token";
const LS_TOKEN_LEGACY = "token";          // compat เก่า
const LS_USER = "auth.user";
const LS_USER_LEGACY = "user";

/* ---------- API base ---------- */
const ENV_BASE =
  (process.env.REACT_APP_API_BASE || "").trim() ||
  (typeof window !== "undefined" && window.__API_BASE__) ||
  (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "");
const API_BASE = ENV_BASE || "";

/* ---------- timing / skew ---------- */
const CLOCK_SKEW_SEC = 60;   // กันเวลาคลาดเคลื่อนระหว่าง client/server
const EXP_MARGIN_SEC = 15;   // margin เพิ่มเติม
const AUTO_REFRESH_BEFORE_SEC = 120; // รีเฟรชก่อนหมดอายุ 2 นาที

/* ---------- utils ---------- */
const isAbs = (url) => /^https?:\/\//i.test(url);
const withBase = (url) => (isAbs(url) ? url : `${API_BASE}${url}`);
const buildQuery = (obj) => {
  if (!obj || typeof obj !== "object") return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) v.forEach((vv) => sp.append(k, String(vv)));
    else sp.append(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
};

/* เราจะผูก callback พวกนี้ตอนสร้าง Provider */
let __onUnauthorized = null;   // เรียกเมื่อ refresh แล้วไม่สำเร็จ -> logout
let __doRefreshOnce = null;    // ฟังก์ชันรีเฟรช 1 ครั้ง คืน access ใหม่หรือ null

/** tiny fetch core ที่ลอง refresh 1 ครั้งอัตโนมัติเมื่อเจอ 401 */
async function requestCore(url, {
  method = "GET", headers, body, query, token, timeoutMs = 15000, raw = false,
} = {}) {
  async function _doFetch(useToken) {
    const fullUrl = withBase(url) + buildQuery(query);
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(new Error("timeout")), timeoutMs);
    try {
      const h = new Headers(headers || {});
      if (useToken) h.set("Authorization", `Bearer ${useToken}`);

      const isForm = typeof FormData !== "undefined" && body instanceof FormData;
      const isURLS = typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams;
      if (body && !isForm && !isURLS && !h.has("Content-Type")) h.set("Content-Type", "application/json");
      if (!h.has("Accept")) h.set("Accept", "application/json, text/plain;q=0.9, */*;q=0.1");

      const toSend = body && !isForm && !isURLS && typeof body !== "string" ? JSON.stringify(body) : body;

      const res = await fetch(fullUrl, {
        method, headers: h, body: toSend, signal: ctl.signal, mode: "cors", credentials: "omit",
      });

      if (!res.ok) {
        let msg = "";
        let parsed;
        try {
          const ct = res.headers.get("content-type") || "";
          if (ct.includes("application/json")) {
            parsed = await res.json();
            msg = parsed?.detail || parsed?.message || JSON.stringify(parsed);
          } else msg = await res.text();
        } catch {}
        const err = new Error(msg || `HTTP ${res.status} ${res.statusText}`);
        err.status = res.status;
        err.response = res;
        err.body = parsed;
        throw err;
      }

      if (raw) return res;
      if (res.status === 204) return null;

      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) return res.json();
      if (ct.startsWith("text/")) return res.text();
      const txt = await res.text();
      try { return JSON.parse(txt); } catch { return txt; }
    } finally {
      clearTimeout(t);
    }
  }

  // ยิงครั้งที่ 1
  try {
    return await _doFetch(token);
  } catch (e) {
    const msg = String(e?.message || "").toLowerCase();
    if (e?.status === 401 || msg.includes("token expired") || msg.includes("not authenticated")) {
      // ลอง refresh 1 ครั้ง หากได้ access ใหม่ -> ยิงซ้ำ
      try {
        const newAccess = typeof __doRefreshOnce === "function" ? await __doRefreshOnce() : null;
        if (newAccess) return await _doFetch(newAccess);
      } catch {}
      // refresh ไม่สำเร็จ -> เคลียร์เซสชัน
      try { typeof __onUnauthorized === "function" && __onUnauthorized(); } catch {}
      const err = new Error("Session expired. Please log in again.");
      err.status = 401;
      throw err;
    }
    // ข้อผิดพลาดอื่น ๆ โยนต่อ
    throw e;
  }
}

/* ---------- JWT helpers ---------- */
function safeBase64UrlToJson(b64url) {
  try {
    const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      Array.prototype.map.call(atob(b64), (c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2)).join("")
    );
    return JSON.parse(json);
  } catch { return null; }
}
function decodeJwt(token) {
  try {
    const parts = String(token || "").split(".");
    if (parts.length < 2) return null;
    return safeBase64UrlToJson(parts[1]);
  } catch { return null; }
}
const getExpMs = (tk) => {
  const p = decodeJwt(tk);
  return p?.exp ? p.exp * 1000 : null;
};
function isExpiredNow(tk, extraAheadSec = 0) {
  const expMs = getExpMs(tk);
  if (!expMs) return true;
  const nowMs = Date.now();
  const ahead = (CLOCK_SKEW_SEC + EXP_MARGIN_SEC + extraAheadSec) * 1000;
  return nowMs >= (expMs - ahead);
}

/* ---------- storage helpers ---------- */
function readAccess() {
  try { return localStorage.getItem(LS_ACCESS) || localStorage.getItem(LS_TOKEN_LEGACY) || ""; }
  catch { return ""; }
}
function writeAccess(tk) {
  try {
    if (!tk) {
      localStorage.removeItem(LS_ACCESS);
      localStorage.removeItem(LS_TOKEN_LEGACY);
    } else {
      localStorage.setItem(LS_ACCESS, tk);
      localStorage.setItem(LS_TOKEN_LEGACY, tk); // compat
    }
  } catch {}
}
function readRefresh() {
  try { return localStorage.getItem(LS_REFRESH) || ""; } catch { return ""; }
}
function writeRefresh(tk) {
  try {
    if (!tk) localStorage.removeItem(LS_REFRESH);
    else localStorage.setItem(LS_REFRESH, tk);
  } catch {}
}
function readUser() {
  try {
    const raw = localStorage.getItem(LS_USER) || localStorage.getItem(LS_USER_LEGACY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function writeUser(u) {
  try {
    if (!u) {
      localStorage.removeItem(LS_USER);
      localStorage.removeItem(LS_USER_LEGACY);
    } else {
      const s = JSON.stringify(u);
      localStorage.setItem(LS_USER, s);
      localStorage.setItem(LS_USER_LEGACY, s);
    }
  } catch {}
}

/* ===================================================================== */
export function AuthProvider({ children }) {
  // access/refresh “ถาวร”
  const [accessToken, setAccessToken] = useState(() => readAccess());
  const [refreshToken, setRefreshToken] = useState(() => readRefresh());
  // ระหว่างขั้น “ยืนยันครั้งแรก”
  const [pendingAccess, setPendingAccess] = useState("");
  const [pendingRefresh, setPendingRefresh] = useState("");
  const [pendingUser, setPendingUser] = useState(null);
  // user ที่ยืนยันแล้วเท่านั้น
  const [user, setUser] = useState(() => readUser());
  const [loading, setLoading] = useState(false);

  /* ----- ตั้ง timer: auto-refresh/auto-logout ----- */
  const timerRef = useRef({ exp: null, refresh: null });
  const clearTimers = () => {
    if (timerRef.current.exp) clearTimeout(timerRef.current.exp);
    if (timerRef.current.refresh) clearTimeout(timerRef.current.refresh);
    timerRef.current.exp = null; timerRef.current.refresh = null;
  };

  const scheduleTimers = (tk) => {
    clearTimers();
    const expMs = getExpMs(tk);
    if (!expMs) return;

    // 1) ตั้งเวลา auto-refresh ก่อนหมดอายุ 2 นาที
    const refreshAt = Math.max(0, expMs - (AUTO_REFRESH_BEFORE_SEC * 1000) - Date.now());
    timerRef.current.refresh = setTimeout(async () => {
      try { await doRefreshOnce(); } catch { /* เงียบไว้; เดี๋ยว 401 ค่อยล้าง */ }
    }, refreshAt);

    // 2) ตั้งเวลา auto-logout ก่อนหมดอายุจริง (รวม skew+margin)
    const logoutAt = Math.max(0, expMs - (CLOCK_SKEW_SEC + EXP_MARGIN_SEC) * 1000 - Date.now());
    timerRef.current.exp = setTimeout(() => doLogout({ silent: true }), logoutAt);
  };

  // ให้ requestCore เรียกเมื่อ refresh ไม่สำเร็จ
  __onUnauthorized = () => doLogout({ silent: true });

  // ฟังก์ชันที่ requestCore จะเรียกเพื่อ refresh 1 ครั้ง แล้วคืน access ใหม่ (หรือ null)
  async function doRefreshOnce() {
    if (!refreshToken) return null;
    try {
      const res = await requestCore("/auth/refresh", {
        method: "POST",
        body: { refresh_token: refreshToken },
      });
      const newAccess = res?.access_token || res?.token || "";
      const newRefresh = res?.refresh_token || ""; // ถ้า BE ทำ rotation
      if (!newAccess) return null;

      setAccessToken(newAccess); writeAccess(newAccess); scheduleTimers(newAccess);
      if (newRefresh) { setRefreshToken(newRefresh); writeRefresh(newRefresh); }
      return newAccess;
    } catch {
      return null;
    }
  }
  __doRefreshOnce = doRefreshOnce;

  // บูต: ถ้า access หมดอายุแล้ว → พยายาม refresh; ถ้าไม่ได้ → ล้าง
  useEffect(() => {
    (async () => {
      if (accessToken) {
        if (isExpiredNow(accessToken)) {
          const ok = await doRefreshOnce();
          if (!ok) await doLogout({ silent: true });
        } else {
          scheduleTimers(accessToken);
        }
      }
    })();
    return clearTimers;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ----- WebSocket (เฉพาะเมื่อ login ถาวรแล้ว) ----- */
  const wsRef = useRef(null);
  const pingRef = useRef(null);
  const wsUrl = useMemo(() => {
    if (!accessToken) return null;
    const base = new URL("/", API_BASE || (typeof window !== "undefined" ? window.location.origin : "http://localhost/"));
    const u = new URL("/ws", base);
    u.searchParams.set("token", accessToken);
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${u.host}${u.pathname}${u.search}`;
  }, [accessToken]);

  useEffect(() => {
    if (!wsUrl) return;
    try { wsRef.current?.close(); } catch {}

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      pingRef.current = setInterval(() => { try { ws.send("ping"); } catch {} }, 25_000);
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "user" && msg.user) {
          setUser(msg.user); writeUser(msg.user);
        } else if (msg.type === "logout") {
          doLogout({ silent: true });
        }
      } catch {}
    };
    ws.onclose = () => {
      wsRef.current = null;
      if (pingRef.current) { clearInterval(pingRef.current); pingRef.current = null; }
    };

    return () => {
      try { ws.close(); } catch {}
      if (pingRef.current) { clearInterval(pingRef.current); pingRef.current = null; }
    };
  }, [wsUrl]);

  /* ---------------- Actions ---------------- */
  async function loginWithEmployeeId(employeeDigits) {
    setLoading(true);
    try {
      const res = await requestCore("/auth/login", {
        method: "POST",
        body: { employee_id: String(employeeDigits || "").trim() },
      });

      // รองรับรูปแบบเก่า/ใหม่
      const access = res?.access_token || res?.token || "";
      const refresh = res?.refresh_token || "";
      const u = res?.user || null;
      const needsConfirm = !!res?.needs_confirm;

      if (!access || !u) throw new Error("Login failed");

      if (needsConfirm) {
        // รอ confirm (ไม่เซ็ตเป็นถาวร)
        setPendingAccess(access);
        setPendingRefresh(refresh || "");
        setPendingUser(u);
        return { step: "confirm", user: u, token: access };
      }

      // ยืนยันแล้ว → login ถาวร
      setAccessToken(access); writeAccess(access); scheduleTimers(access);
      setRefreshToken(refresh || ""); writeRefresh(refresh || "");
      setUser(u); writeUser(u);
      setPendingAccess(""); setPendingRefresh(""); setPendingUser(null);
      return { step: "ok", user: u };
    } catch (e) {
      return Promise.reject(new Error(e?.message || "Login error"));
    } finally {
      setLoading(false);
    }
  }

  async function confirmFirstLogin(_userId, displayName, email) {
    const useAccess = pendingAccess || accessToken;
    if (!useAccess) throw new Error("Not authenticated");

    const updated = await requestCore("/users/me", {
      method: "PUT",
      body: { name: displayName, email },
      token: useAccess,
    });

    // โปรโมตเป็น session ถาวร (กรณีมาจาก pending)
    if (!accessToken && pendingAccess) {
      setAccessToken(useAccess); writeAccess(useAccess); scheduleTimers(useAccess);
      if (pendingRefresh) { setRefreshToken(pendingRefresh); writeRefresh(pendingRefresh); }
    }
    setPendingAccess(""); setPendingRefresh(""); setPendingUser(null);

    setUser(updated); writeUser(updated);
    return updated;
  }

  async function doLogout({ silent = false } = {}) {
    try {
      if (!silent && accessToken) await requestCore("/auth/logout", { method: "POST", token: accessToken });
    } catch { /* ignore */ }

    setAccessToken(""); writeAccess("");
    setRefreshToken(""); writeRefresh("");
    setUser(null); writeUser(null);

    setPendingAccess(""); setPendingRefresh(""); setPendingUser(null);

    try { wsRef.current?.close(); } catch {}
    if (pingRef.current) { clearInterval(pingRef.current); pingRef.current = null; }
    clearTimers();
  }

  async function refreshMe() {
    if (!accessToken) return null;
    try {
      const me = await requestCore("/auth/me", { token: accessToken });
      setUser(me); writeUser(me);
      return me;
    } catch (e) {
      await doLogout({ silent: true });
      throw e;
    }
  }

  // บูตอีกชั้น: ถ้ามี access แต่ยังไม่มี user → ลองโหลด me
  useEffect(() => {
    (async () => {
      if (accessToken && !user) {
        try {
          if (isExpiredNow(accessToken)) {
            const ok = await doRefreshOnce();
            if (!ok) return await doLogout({ silent: true });
          }
          await refreshMe();
          scheduleTimers(accessToken);
        } catch {
          await doLogout({ silent: true });
        }
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value = useMemo(() => ({
    token: accessToken,                // ชื่อเดิม (บางไฟล์ยังใช้) = access_token
    accessToken,
    refreshToken,
    user,
    isAuthed: !!accessToken && !!user, // auth สำเร็จจริงต้องมีทั้ง access + user
    pendingUser,
    loading,
    loginWithEmployeeId,
    confirmFirstLogin,
    cancelPendingLogin: () => { setPendingAccess(""); setPendingRefresh(""); setPendingUser(null); },
    logout: (opts) => doLogout(opts || {}),
    refreshMe,
    API_BASE,
    requestCore,                       // เผื่อบาง component อยากใช้โดยตรง (จะ auto refresh ให้อยู่แล้ว)
  }), [accessToken, refreshToken, user, pendingUser, loading]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
