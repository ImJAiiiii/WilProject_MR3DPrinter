// src/api/auth.js

// Dev ยิงไปพอร์ต 8000, โปรดักชันให้ใช้ origin เดียวกับเว็บ (path เปล่า)
export const API_BASE =
  process.env.NODE_ENV === "development" ? "http://localhost:8000" : "";

/** อ่าน error จาก response ให้เป็นข้อความมนุษย์อ่านได้ */
async function readErr(res) {
  try {
    const j = await res.json();
    return j?.detail || JSON.stringify(j);
  } catch {
    try {
      return await res.text();
    } catch {
      return `${res.status} ${res.statusText}`;
    }
  }
}

/** fetch รวมศูนย์ + timeout + แปลง JSON อัตโนมัติ */
async function fetchJSON(path, init = {}, timeoutMs = 15000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort("timeout"), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, { signal: ctl.signal, ...init });
    if (!res.ok) throw new Error(await readErr(res));
    if (res.status === 204) return null;
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  } finally {
    clearTimeout(t);
  }
}

/** ทำให้ผลลัพธ์ login เข้ากันได้ย้อนหลังเสมอ */
function normalizeLoginResponse(obj) {
  if (!obj || typeof obj !== "object") return obj;

  const access =
    obj.access_token || obj.token || obj?.tokens?.access_token || null;
  const refresh =
    obj.refresh_token || obj?.tokens?.refresh_token || null;

  return {
    // ===== คงค่าเดิมทั้งหมดไว้ =====
    ...obj,
    // ===== เพิ่ม/ซ้ำฟิลด์เพื่อความเข้ากันได้ =====
    access_token: access,
    refresh_token: refresh,
    token: access, // FE เก่าบางจุดยังอ่าน field 'token'
    token_type: obj.token_type || "bearer",
  };
}

/** login: ส่ง employee_id แล้วได้ access/refresh token กลับมา */
export async function apiLogin(employeeId) {
  const raw = await fetchJSON("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ employee_id: String(employeeId || "").trim() }),
  });
  return normalizeLoginResponse(raw);
}

/** เรียก /auth/refresh เพื่อขอ access token ใหม่ */
export async function apiRefresh(refreshToken) {
  const res = await fetchJSON("/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  // บาง BE อาจคืนเฉพาะ access_token
  const access = res?.access_token || res?.token || null;
  const refresh = res?.refresh_token || null;
  return {
    ...res,
    access_token: access,
    refresh_token: refresh,
    token: access,
    token_type: res?.token_type || "bearer",
  };
}

/** me: อ่านโปรไฟล์ปัจจุบัน */
export async function apiMe(token) {
  return fetchJSON("/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/** logout (stateless ที่ฝั่ง BE) */
export async function apiLogout(token) {
  return fetchJSON("/auth/logout", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
}

/** อัปเดตชื่อ/อีเมล + confirm */
export async function apiUpdateMe(token, { name, email }) {
  return fetchJSON("/users/me", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ name, email }),
  });
}

/* ===== Optional helper สำหรับส่วนอื่น ๆ ของแอพ ===== */

export function hasTokenPair(loginResult) {
  return Boolean(
    loginResult &&
      (loginResult.access_token || loginResult.token) &&
      loginResult.refresh_token
  );
}

export function getAccessToken(loginResult) {
  return loginResult?.access_token || loginResult?.token || null;
}

export function getRefreshToken(loginResult) {
  return loginResult?.refresh_token || null;
}
