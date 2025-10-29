// src/history.js
const LS_HISTORY = "userHistory";
const MAX_ITEMS_PER_USER = 200;

// =========================
// Utility
// =========================
const safeStr = (v) => (v == null ? "" : String(v));

function safeJSONParse(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

// =========================
// LocalStorage (offline)
// =========================
export function loadHistoryMap() {
  const raw = localStorage.getItem(LS_HISTORY);
  const obj = safeJSONParse(raw);
  return typeof obj === "object" && obj ? obj : {};
}

export function saveHistoryMap(map) {
  try {
    localStorage.setItem(LS_HISTORY, JSON.stringify(map));
  } catch {}
}

export function newId() {
  try {
    return crypto.randomUUID();
  } catch {
    return "id_" + Date.now() + "_" + Math.random().toString(16).slice(2);
  }
}

/**
 * บันทึก 1 รายการเข้า history ของ userId
 * - append ด้านหน้า (unshift)
 * - limit 200
 */
export function appendHistory(userId, item) {
  const map = loadHistoryMap();
  const k = safeStr(userId);
  const arr = Array.isArray(map[k]) ? map[k] : [];

  const normalized = {
    id: newId(),
    uploadedAt: new Date().toISOString(),
    name: item?.name || item?.file?.name || "Unnamed",
    thumb: item?.thumb || item?.file?.thumb || item?.template?.preview || "/images/3D.png",
    template: item?.template ?? null,
    stats: item?.stats ?? null,
    file: item?.file ?? null,
    source: item?.source || "upload",
    gcode_key: item?.gcode_key || null,
    gcode_path: item?.gcode_path || null,
  };

  arr.unshift(normalized);
  while (arr.length > MAX_ITEMS_PER_USER) arr.pop();

  map[k] = arr;
  saveHistoryMap(map);
}

export function getUserHistoryLocal(userId) {
  const map = loadHistoryMap();
  const arr = Array.isArray(map[safeStr(userId)]) ? map[safeStr(userId)] : [];
  return arr;
}

// =========================
// Backend API Integration
// =========================
export async function fetchUserHistory(apiBase, token, { limit = 200 } = {}) {
  try {
    const url = `${apiBase.replace(/\/$/, "")}/api/history/my?limit=${limit}`;
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();

    // ตรวจสอบว่าเป็น array จริง
    if (!Array.isArray(data)) throw new Error("Invalid response format");
    console.log(`[history] fetched ${data.length} records from backend`);
    return data;
  } catch (err) {
    console.warn("⚠️ fetchUserHistory failed → fallback to localStorage:", err);
    return [];
  }
}

// =========================
// Combined Loader
// =========================
export async function loadHistory(userId, apiBase, token) {
  const backendData = await fetchUserHistory(apiBase, token);
  if (backendData.length > 0) {
    return backendData;
  }
  // fallback to local storage
  return getUserHistoryLocal(userId);
}
export const getUserHistory = getUserHistoryLocal;