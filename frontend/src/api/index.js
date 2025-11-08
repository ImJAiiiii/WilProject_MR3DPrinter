// src/api/index.js
import { useMemo } from "react";
import { useAuth } from "../auth/AuthContext";

/**
 * ลำดับแหล่ง API base:
 *  1) window.__API_BASE__ (runtime override)
 *  2) REACT_APP_API_BASE  (.env ตอน build)
 *  3) ค่าว่าง ""          (= same-origin / proxy)
 */
let RUNTIME_API_BASE =
  (typeof window !== "undefined" && window.__API_BASE__) ||
  (process.env.REACT_APP_API_BASE && process.env.REACT_APP_API_BASE.trim()) ||
  "";

/** เปลี่ยน API base ระหว่างรัน */
export function setApiBase(url) {
  try {
    if (typeof url !== "string") return;
    RUNTIME_API_BASE = url.trim();
    if (typeof window !== "undefined") window.__API_BASE__ = RUNTIME_API_BASE;
    /* eslint-disable no-console */
    console.info("[api] API_BASE set to:", RUNTIME_API_BASE || "<same-origin>");
  } catch {}
}

export const API_BASE = RUNTIME_API_BASE;

/* ---------------- utils ---------------- */
const ABS_HTTP = /^https?:\/\//i;
const numberIsFinite = (x) =>
  typeof Number.isFinite === "function" ? Number.isFinite(x) : isFinite(x);

const toQ = (v) => {
  if (typeof v === "boolean") return v ? "1" : "0";
  if (v === null || v === undefined) return undefined;
  return String(v);
};

/** สร้าง URL อย่างปลอดภัย พร้อมรวม query เดิมกับ query ใหม่ */
function buildUrl(path, query) {
  const rel = ABS_HTTP.test(path) ? path : (path.startsWith("/") ? path : `/${path}`);

  const base = RUNTIME_API_BASE || "";
  const origin =
    (typeof window !== "undefined" && window.location?.origin) || "http://localhost";
  const baseForURL = base ? (base.endsWith("/") ? base : base + "/") : origin;

  const u = new URL(rel, baseForURL);
  if (query && typeof query === "object") {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null) continue;
      if (Array.isArray(v)) v.forEach((vv) => u.searchParams.append(k, toQ(vv)));
      else {
        const qv = toQ(v);
        if (qv !== undefined) u.searchParams.set(k, qv);
      }
    }
  }
  return u.toString();
}

/** แปลงเป็น absolute URL จาก BASE (รองรับ same-origin) */
function toAbs(url) {
  if (ABS_HTTP.test(url)) return url;
  const rel = url.startsWith("/") ? url : `/${url}`;
  const base = RUNTIME_API_BASE || "";
  const origin =
    (typeof window !== "undefined" && window.location?.origin) || "http://localhost";
  const baseForURL = base ? (base.endsWith("/") ? base : base + "/") : origin;
  return new URL(rel, baseForURL).toString();
}

/* ---------------- helper URLs สำหรับรูป/พรีวิว ---------------- */
/** แปลง S3 object key -> URL เสิร์ฟไฟล์ผ่าน BE (รองรับ token ผ่าน query) */
export function objectKeyToUrl(keyOrUrl, token) {
  if (!keyOrUrl) return "";
  if (ABS_HTTP.test(keyOrUrl)) return keyOrUrl;
  const u = new URL(
    "/files/raw",
    RUNTIME_API_BASE ||
      (typeof window !== "undefined" ? window.location.origin : "http://localhost")
  );
  u.searchParams.set("object_key", keyOrUrl);
  if (token) u.searchParams.set("token", token);
  return u.toString();
}

/** URL เรนเดอร์สดจาก .gcode (ใช้ fallback เวลาโหลด .preview.png พัง) */
export function renderUrlFromGcodeKey(gcodeKey, { size = "1600x1200", hide = "none" } = {}) {
  if (!gcodeKey) return "";
  const u = new URL(
    "/preview/render",
    RUNTIME_API_BASE ||
      (typeof window !== "undefined" ? window.location.origin : "http://localhost")
  );
  u.searchParams.set("object_key", gcodeKey);
  u.searchParams.set("size", size);
  u.searchParams.set("hide", hide);
  return u.toString();
}

/** ทางลัด legacy: เผื่อบางหน้าเรียก /catalog/* (ควรเลี่ยง แต่ให้ไว้) */
export function legacyCatalogUrl(pathLike, token) {
  if (!pathLike) return "";
  if (ABS_HTTP.test(pathLike)) return pathLike;
  const rel = pathLike.startsWith("/") ? pathLike : `/${pathLike}`;
  const u = new URL(
    rel,
    RUNTIME_API_BASE ||
      (typeof window !== "undefined" ? window.location.origin : "http://localhost")
  );
  if (token) u.searchParams.set("token", token);
  return u.toString();
}

/* ---------- error helpers ---------- */
async function parseErrorResponse(res, fallback = "") {
  let msg = fallback;
  let parsed;
  try {
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      parsed = await res.json();
      if (Array.isArray(parsed?.detail)) {
        msg = parsed.detail
          .map((d) => {
            const loc = Array.isArray(d?.loc) ? d.loc.join(".") : d?.loc;
            return [d?.type, loc, d?.msg].filter(Boolean).join(" | ");
          })
          .join("; ");
      } else {
        msg = parsed?.detail || parsed?.message || JSON.stringify(parsed);
      }
    } else {
      msg = (await res.text()) || fallback;
    }
  } catch {}
  const err = new Error(msg || `HTTP ${res.status} ${res.statusText}`);
  err.status = res.status;
  err.statusText = res.statusText;
  err.body = parsed;
  err.response = res;
  return err;
}

/* ---------------- retry/backoff core ---------------- */
async function fetchWithRetry(
  doFetch,
  { retries = 0, backoffMs = 800, maxBackoffMs = 8000, retryOn } = {}
) {
  let attempt = 0;
  let delay = backoffMs;

  const shouldRetry = (err) => {
    if (typeof retryOn === "function") return retryOn(err);
    if (Array.isArray(retryOn)) {
      const code = err?.status ?? err?.response?.status ?? 0;
      return retryOn.includes(code);
    }
    // ค่าเริ่มต้น: เน็ตหลุด/timeout, 429, 5xx, และ 0 (unknown)
    const code = err?.status ?? err?.response?.status ?? 0;
    const msg = String(err?.message || "");
    const isNet = code === 0 || /Network error|Failed to fetch|timeout/i.test(msg);
    return isNet || code === 429 || (code >= 500 && code <= 599);
  };

  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      return await doFetch();
    } catch (e) {
      if (attempt >= retries || !shouldRetry(e)) throw e;
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, maxBackoffMs);
      attempt += 1;
    }
  }
}

/* helper: ค่า retry เริ่มต้นตาม method */
function defaultRetriesForMethod(method) {
  const m = String(method || "GET").toUpperCase();
  if (m === "GET" || m === "HEAD") return 2; // ปลอดภัยในการ retry
  return 0;
}

/* ---------------- API factory ----------------
   เปลี่ยนมา “พึ่งพา” AuthContext.requestCore เสมอ (auto-refresh)
------------------------------------------------ */
export function makeApi({ token, onUnauthorized, requestCoreFn } = {}) {
  if (typeof requestCoreFn !== "function") {
    throw new Error("[api] makeApi requires requestCoreFn (from AuthContext)");
  }

  async function request(
    url,
    {
      method = "GET",
      headers,
      body,
      query,
      timeout,
      timeoutMs,
      raw = false,
      expect,
      auth = true,
      authQuery = false,
      credentials,
      mode = "cors",
      // retry options
      retries,
      backoffMs,
      maxBackoffMs,
      retryOn,
    } = {}
  ) {
    const doRequest = async () => {
      const q = { ...(query || {}) };
      const useToken = token || "";

      if (authQuery && useToken) q.token = useToken;

      const fullUrl = buildUrl(url, q);
      const _timeout = numberIsFinite(timeoutMs)
        ? timeoutMs
        : numberIsFinite(timeout)
        ? timeout
        : 30000; // default 30s

      try {
        const isForm = typeof FormData !== "undefined" && body instanceof FormData;
        const isURLSP = typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams;
        const bodyToSend =
          body && !isForm && !isURLSP && typeof body !== "string" ? JSON.stringify(body) : body;

        const res = await requestCoreFn(fullUrl, {
          method,
          headers,
          body: bodyToSend,
          token: auth ? useToken : undefined,
          timeoutMs: _timeout,
          raw,
        });

        if (!raw) {
          if (expect === "blob") return res; // เมื่อ raw=true ใน requestCoreFn จะคืน Response
          if (expect === "text") return res;
        }
        return res;
      } catch (e) {
        if (e?.response instanceof Response) {
          throw await parseErrorResponse(e.response);
        }
        throw e;
      }
    };

    const useRetries =
      numberIsFinite(retries) ? retries : defaultRetriesForMethod(method || "GET");

    if (useRetries > 0) {
      return fetchWithRetry(doRequest, {
        retries: useRetries,
        backoffMs: backoffMs ?? 800,
        maxBackoffMs: maxBackoffMs ?? 8000,
        retryOn:
          retryOn ??
          ((err) => {
            const code = err?.status ?? err?.response?.status ?? 0;
            const msg = String(err?.message || "");
            const isNet = code === 0 || /Network error|Failed to fetch|timeout/i.test(msg);
            return isNet || code === 429 || [502, 503, 504].includes(code) || (code >= 500 && code <= 599);
          }),
      });
    }
    return doRequest();
  }

  // shorthand
  const get = (url, query, opts) => request(url, { method: "GET", query, ...(opts || {}) });
  const del = (url, query, opts) => request(url, { method: "DELETE", query, ...(opts || {}) });
  const post = (url, body, query, opts) =>
    request(url, { method: "POST", body, query, ...(opts || {}) });
  const put = (url, body, query, opts) =>
    request(url, { method: "PUT", body, query, ...(opts || {}) });
  const patch = (url, body, query, opts) =>
    request(url, { method: "PATCH", body, query, ...(opts || {}) });

  // uploads (ใช้กับ backend ปกติ ไม่ใช่ presigned)
  const upload = (url, formData, opts) =>
    request(url, {
      method: "POST",
      body: formData,
      ...(opts || {}),
      headers: { ...(opts?.headers || {}) }, // อย่าเซ็ต Content-Type เอง (ให้ browser ใส่ boundary)
    });

  // อัปโหลดไป Presigned URL (S3/MinIO) — ไม่ใส่ auth/credentials
  const uploadPresigned = (presignedUrl, fileOrBlob, opts = {}) =>
    request(toAbs(presignedUrl), {
      method: "PUT",
      body: fileOrBlob,
      headers: opts.headers || {},
      timeout: opts.timeout ?? opts.timeoutMs ?? 300000, // 5 นาที
      raw: true,
      auth: false,
      credentials: "omit",
      retries: numberIsFinite(opts.retries) ? opts.retries : 0,
      mode: "cors",
    });

  async function download(url, filename, opts) {
    const res = await request(url, { ...(opts || {}), raw: true });
    const blob = await res.blob();
    let dlName = filename || "";
    try {
      const cd = res.headers.get("content-disposition") || "";
      const m =
        /filename\*?=(?:UTF-8'')?([^;]+)/i.exec(cd) || /filename="?([^"]+)"?/i.exec(cd);
      if (m) dlName = decodeURIComponent(m[1].replace(/(^"|"$)/g, ""));
    } catch {}
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = dlName;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 0);
  }

  // ---------- Domain APIs ----------
  const files = {
    raw: (object_key, opts) =>
      get("/files/raw", { object_key }, { ...(opts || {}), expect: "blob" }),
    head: (object_key, opts) => get("/files/head", { object_key }, opts),
    exists: (object_key, opts) => get("/files/exists", { object_key }, opts),

    /** สร้าง URL โหลดไฟล์จาก MinIO ผ่าน BE (แนบ token อัตโนมัติ + cache-buster) */
    rawUrl(object_key, tkn) {
      if (!object_key) return "";
      const base =
        RUNTIME_API_BASE ||
        (typeof window !== "undefined" ? window.location.origin : "http://localhost");
      const u = new URL("/files/raw", base);
      u.searchParams.set("object_key", object_key);
      const tok = tkn || token || "";
      if (tok) u.searchParams.set("token", tok);
      u.searchParams.set("t", Date.now().toString());
      return u.toString();
    },
  };

  const storage = {
    // === เปลี่ยนเส้นทางทั้งหมดเป็น /api/storage/... ===
    requestUpload: (arg1, content_type, size, opts) => {
      const body = typeof arg1 === "object" ? arg1 : { filename: arg1, content_type, size };
      return post("/api/storage/upload/request", body, undefined, {
        timeout: 45000,
        retries: 2,
        ...(opts || {}),
      });
    },
    completeUpload: (payload, opts) =>
      post("/api/storage/upload/complete", payload, undefined, {
        timeout: 45000,
        retries: 1,
        ...(opts || {}),
      }),
    finalize: (payload, opts) =>
      post("/api/storage/finalize", payload, undefined, {
        timeout: 45000,
        retries: 1,
        ...(opts || {}),
      }),

    // lists
    listMine: (query, opts) => get("/api/storage/my", query, opts),
    listAll: (query, opts) => get("/api/storage", query, opts),
    listByUser: (employee_id, query, opts) =>
      get(`/api/storage/by-user/${encodeURIComponent(employee_id)}`, query, opts),

    // get / head / presign
    getById: (id, opts) => get(`/api/storage/id/${encodeURIComponent(id)}`, undefined, opts),
    head: (object_key, opts) => get("/api/storage/head", { object_key }, opts),
    presignGet: (object_key, with_meta = false, opts) =>
      get("/api/storage/presign", { object_key, with_meta }, opts),

    // deletes
    deleteById: (id, { deleteFromS3 = true } = {}, opts) =>
      del(`/api/storage/id/${encodeURIComponent(id)}`, { delete_object_from_s3: deleteFromS3 }, opts),

    deleteByKey: (object_key, { deleteFromS3 = true } = {}, opts) =>
      del("/api/storage/by-key", { object_key, delete_object_from_s3: deleteFromS3 }, opts),

    deleteMine: ({ olderThanDays, deleteFromS3 = true } = {}, opts) =>
      del("/api/storage/my", { older_than_days: olderThanDays, delete_object_from_s3: deleteFromS3 }, opts),

    // --- helpers สำหรับไฟล์ manifest (.json) คู่กับ .gcode ---
    manifestKeyFor(gcodeKey) {
      if (!gcodeKey) return null;
      return String(gcodeKey).replace(/\.[^.]+$/i, "") + ".json";
    },
    manifestPresign(gcodeKey, opts) {
      const mk = this.manifestKeyFor(gcodeKey);
      if (!mk) return Promise.resolve(null);
      return this.presignGet(mk, false, opts);
    },

    // --- helpers สำหรับ PNG preview (.preview.png) ข้าง ๆ .gcode ---
    previewKeyFor(gcodeKey) {
      if (!gcodeKey) return null;
      const stem = String(gcodeKey).replace(/\.[^.]+$/i, "");
      return `${stem}.preview.png`;
    },
    async previewPresign(gcodeKey, opts) {
      const pk = this.previewKeyFor(gcodeKey);
      if (!pk) return null;
      try {
        const r = await this.presignGet(pk, false, opts);
        return r?.url || null;
      } catch {
        return null;
      }
    },

    // --- regenerate preview PNG บน BE (การันตีสร้างใหม่) ---
    regeneratePreview(gcodeKey, opts) {
      return post("/api/storage/preview/regenerate", null, { object_key: gcodeKey }, {
        timeout: 45000,
        retries: 1,
        ...(opts || {}),
      });
    },
  };

  const slicer = {
    preview: (payload, opts) =>
      post("/api/slicer/preview", payload, undefined, {
        timeout: 60000,
        retries: 0, // งานหนัก ไม่ควร retry อัตโนมัติ
        ...(opts || {}),
      }),
    thumbnail: (object_key, opts) => get("/api/slicer/thumbnail", { object_key }, opts),
  };

  const queue = {
    list: (printer_id, include_all = true, opts) =>
      get(
        `/printers/${encodeURIComponent(printer_id)}/queue`,
        { include_all: include_all ? 1 : 0 },
        { timeout: 20000, retries: 2, ...(opts || {}) }
      ),
    create: (payload, printer_id, opts) =>
      post("/api/print", payload, printer_id ? { printer_id } : undefined, {
        timeout: 45000,
        retries: 0,
        ...(opts || {}),
      }),
    cancel: (printer_id, job_id, opts) =>
      post(
        `/printers/${encodeURIComponent(printer_id)}/queue/${encodeURIComponent(job_id)}/cancel`,
        null,
        undefined,
        { timeout: 15000, retries: 1, ...(opts || {}) }
      ),
    current: (printer_id, opts) =>
      get(`/printers/${encodeURIComponent(printer_id)}/current-job`, undefined, {
        timeout: 15000,
        retries: 2,
        ...(opts || {}),
      }),
  };

  const history = {
    listMine: (query, opts) => get("/history/my", query, opts),
    merge: (items, opts) => post("/history/merge", { items }, undefined, opts),
  };

  // OctoPrint / Printer helpers
  const printer = {
    status: (printer_id, opts) =>
      get(`/printers/${encodeURIComponent(printer_id)}/status`, undefined, {
        timeout: 15000,
        retries: 2,
        ...(opts || {}),
      }),

    snapshot: (printer_id, opts) =>
      get(`/printers/${encodeURIComponent(printer_id)}/snapshot`, undefined, {
        ...(opts || {}),
        expect: "blob",
        timeout: 10000,
        retries: 1,
      }),

    pause: (printer_id, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/pause`, null, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    cancel: (printer_id, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/cancel`, null, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    resume: (printer_id, opts) =>
      post(
        `/printers/${encodeURIComponent(printer_id)}/octoprint/command`,
        { command: "pause", action: "resume" },
        undefined,
        { timeout: 15000, retries: 0, ...(opts || {}) }
      ),

    octoprintJob: (printer_id, opts) =>
      get(`/printers/${encodeURIComponent(printer_id)}/octoprint/job`, undefined, {
        timeout: 15000,
        retries: 2,
        ...(opts || {}),
      }),

    async octoprintJobSafe(printer_id, opts) {
      const now = Date.now();
      const nextOctoRetryMap = (printer._nextOctoRetryAt ||= new Map());
      const nextAt = nextOctoRetryMap.get(printer_id) || 0;
      if (now < nextAt) return null;
      try {
        return await request(`/printers/${encodeURIComponent(printer_id)}/octoprint/job`, {
          ...(opts || {}),
          retries: 2,
          backoffMs: 800,
          maxBackoffMs: 4000,
          retryOn: [502, 503, 504],
          timeout: 15000,
          method: "GET",
        });
      } catch (e) {
        if ([502, 503, 504].includes(Number(e?.status))) {
          nextOctoRetryMap.set(printer_id, now + 60_000);
        }
        throw e;
      }
    },

    temps: (printer_id, opts) =>
      get(`/printers/${encodeURIComponent(printer_id)}/octoprint/temps`, undefined, {
        timeout: 15000,
        retries: 2,
        ...(opts || {}),
      }),

    setTemperature: (printer_id, { nozzle, bed } = {}, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/octoprint/temperature`, { nozzle, bed }, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    setFeedrateAlias: (printer_id, factor, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/octoprint/feedrate`, { factor }, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    setToolTemp: (printer_id, target, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/temp/tool`, { target }, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    setBedTemp: (printer_id, target, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/temp/bed`, { target }, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),

    setFeedrate: (printer_id, factor, opts) =>
      post(`/printers/${encodeURIComponent(printer_id)}/speed`, { factor }, undefined, {
        timeout: 15000,
        retries: 0,
        ...(opts || {}),
      }),
  };

  /* -------- Notifications API -------- */
  const notifications = {
    list: (query, opts) =>
      get("/api/notifications", query, {
        timeout: 15000,
        retries: 2,
        ...(opts || {}),
      }),
    markAllRead: (opts) =>
      post("/api/notifications/mark-all-read", null, undefined, {
        timeout: 10000,
        retries: 0,
        ...(opts || {}),
      }),
    remove: (id, opts) =>
      del(`/api/notifications/${encodeURIComponent(id)}`, undefined, {
        timeout: 10000,
        retries: 0,
        ...(opts || {}),
      }),
  };

  // SSE / WS
  function sse(path, { query, withToken = true } = {}) {
    const q = { ...(query || {}) };
    const tok = token || "";
    if (withToken && tok) q.token = tok;
    const url = buildUrl(path, q);
    return new EventSource(url);
  }
  function sseWithBackoff(path, { query, withToken = true, onMessage, onOpen, onError } = {}) {
    let es = null,
      stopped = false,
      backoff = 1000;
    const start = () => {
      if (stopped) return;
      try {
        es = sse(path, { query, withToken });
        es.onopen = (e) => {
          backoff = 1000;
          onOpen?.(e);
        };
        es.onmessage = (e) => onMessage?.(e);
        es.onerror = (e) => {
          onError?.(e);
          es?.close();
          if (!stopped) {
            setTimeout(start, backoff);
            backoff = Math.min(backoff * 2, 30000);
          }
        };
      } catch (e) {
        onError?.(e);
        setTimeout(start, backoff);
        backoff = Math.min(backoff * 2, 30000);
      }
    };
    start();
    return { close() { stopped = true; try { es?.close(); } catch {} } };
  }
  function wsUrl(path = "/ws") {
    const httpBase =
      (RUNTIME_API_BASE ||
        (typeof window !== "undefined" ? window.location.origin : "http://localhost")).replace(
        /\/+$/,
        ""
      ) + "/";
    const u = new URL(path, httpBase);
    const tok = token || "";
    if (tok) u.searchParams.set("token", tok);
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${u.host}${u.pathname}${u.search}`;
  }

  return {
    API_BASE: RUNTIME_API_BASE,
    request,
    get,
    post,
    put,
    patch,
    del,
    download,
    upload,
    uploadPresigned,
    files,
    storage,
    slicer,
    queue,
    printer,
    history,
    notifications,
    sse,
    sseWithBackoff,
    wsUrl,
  };
}

/* ---------------- React hook wrapper (stateful) ----------------
   ส่ง requestCore (จาก AuthContext) เข้าไปให้ makeApi
----------------------------------------------------------------- */
export function useApi() {
  const { token, logout, requestCore } = useAuth() || {};
  return useMemo(
    () =>
      makeApi({
        token,
        onUnauthorized: () => logout?.({ silent: true }),
        requestCoreFn: requestCore, // สำคัญ: ให้ request วิ่งผ่านตัวที่ auto-refresh
      }),
    [token, logout, requestCore]
  );
}
