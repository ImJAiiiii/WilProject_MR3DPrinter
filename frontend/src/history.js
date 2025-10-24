// src/history.js
const LS_HISTORY = "userHistory";
const MAX_ITEMS_PER_USER = 200;

// ปลอดภัยกับค่าที่ไม่ใช่สตริง
const safeStr = (v) => (v == null ? "" : String(v));

export function loadHistoryMap() {
  try {
    const raw = localStorage.getItem(LS_HISTORY);
    const obj = raw ? JSON.parse(raw) : {};
    return typeof obj === "object" && obj ? obj : {};
  } catch {
    return {};
  }
}

export function saveHistoryMap(map) {
  try {
    localStorage.setItem(LS_HISTORY, JSON.stringify(map));
  } catch {}
}

// ใช้ UUID จริงถ้ามี ให้ fallback เป็น timestamp
export function newId() {
  try { return crypto.randomUUID(); } catch { return "id_" + Date.now() + "_" + Math.random().toString(16).slice(2); }
}

/**
 * บันทึก 1 รายการเข้า history ของ userId
 * - append ด้านหน้า (unshift)
 * - ไม่ใช้ object_key/name เป็น id (กันชนกัน) => ใช้ UUID แยกแต่ละครั้ง
 * - เก็บ template/stats/file ให้ครบ เพื่อให้หน้าประวัติแสดงได้เต็ม
 */
export function appendHistory(userId, item) {
  const map = loadHistoryMap();
  const k = safeStr(userId);
  const arr = Array.isArray(map[k]) ? map[k] : [];

  const normalized = {
    id: newId(),                // <— id ใหม่ทุกครั้ง กันทับ
    uploadedAt: new Date().toISOString(),
    name: item?.name || item?.file?.name || "Unnamed",
    thumb: item?.thumb || item?.file?.thumb || item?.template?.preview || "/images/3D.png",
    // เก็บ template/stats/file ต้นฉบับไว้เต็ม ๆ
    template: item?.template ?? null,
    stats:    item?.stats ?? null,
    file:     item?.file ?? null,
    // เผื่อใช้กับ reprint/queue
    source:   item?.source || "upload",
    gcode_key: item?.gcode_key || null,
    gcode_path: item?.gcode_path || null,
  };

  // ไม่ dedupe ตาม object_key เพื่อ “ไม่ทับ” งานเก่า
  arr.unshift(normalized);

  // limit เพื่อกันโตเกินไป
  while (arr.length > MAX_ITEMS_PER_USER) arr.pop();

  map[k] = arr;
  saveHistoryMap(map);
}

/** ดึงรายการของผู้ใช้เดียว (array) */
export function getUserHistory(userId) {
  const map = loadHistoryMap();
  const arr = Array.isArray(map[safeStr(userId)]) ? map[safeStr(userId)] : [];
  return arr;
}
