// src/utils/devReset.js

/**
 * ล้างแคชฝั่ง FE ให้หมดจด พร้อมแจ้งทุกแท็บให้รีโหลดเองได้
 * - ลบ localStorage keys หลัก: userHistory, customStorage, token, auth.token
 * - ลบ sessionStorage: lastQueuedJobId
 * - (ตัวเลือก) ลบ IndexedDB และ Cache Storage (PWA)
 *
 * @param {Object} opts
 * @param {boolean} [opts.clearTokens=true]   ลบ token ใน localStorage
 * @param {boolean} [opts.clearIndexedDB=false] ลบฐาน IndexedDB ทั้งหมด (ถ้าบราวเซอร์รองรับ)
 * @param {boolean} [opts.clearCaches=false]  ลบ Cache Storage ทั้งหมด (ถ้ามี service worker)
 * @param {boolean} [opts.broadcast=true]     ส่ง StorageEvent ให้แท็บอื่นๆ รับรู้
 */
export async function resetLocalCaches(opts = {}) {
  const {
    clearTokens = true,
    clearIndexedDB = false,
    clearCaches = false,
    broadcast = true,
  } = opts;

  // ---- localStorage / sessionStorage ----
  const LS_KEYS = ['userHistory', 'customStorage'];
  if (clearTokens) {
    LS_KEYS.push('token', 'auth.token'); // รองรับทั้งคีย์เก่า/ใหม่
  }

  try {
    for (const k of LS_KEYS) localStorage.removeItem(k);
  } catch {}

  try {
    sessionStorage.removeItem('lastQueuedJobId');
  } catch {}

  // แจ้งทุกแท็บให้รู้ว่ามีการรีเซ็ต (บางเบราว์เซอร์ไม่ยอมยิง event เองเวลาลบในแท็บเดียวกัน)
  if (broadcast && typeof window !== 'undefined') {
    try {
      // ยิงทีละ key เผื่อ UI บางจุดฟัง key เฉพาะ
      for (const k of LS_KEYS) {
        window.dispatchEvent(new StorageEvent('storage', { key: k }));
      }
      window.dispatchEvent(new StorageEvent('storage', { key: 'RESET_ALL' }));
    } catch {}
  }

  // ---- IndexedDB (optional) ----
  if (clearIndexedDB && typeof indexedDB !== 'undefined') {
    try {
      if (indexedDB.databases) {
        const dbs = await indexedDB.databases();
        await Promise.all(
          (dbs || []).map((d) =>
            d && d.name ? new Promise((res) => {
              const req = indexedDB.deleteDatabase(d.name);
              req.onsuccess = req.onerror = req.onblocked = () => res();
            }) : Promise.resolve()
          )
        );
      } else {
        // ถ้าไม่รองรับ indexedDB.databases() ให้ลบชื่อที่เรารู้จักเอง
        const known = ['keyval-store', 'workbox-precache-v2']; // เติมชื่อแอปถ้ารู้
        await Promise.all(
          known.map((name) => new Promise((res) => {
            const req = indexedDB.deleteDatabase(name);
            req.onsuccess = req.onerror = req.onblocked = () => res();
          }))
        );
      }
    } catch {}
  }

  // ---- Cache Storage (optional, PWA) ----
  if (clearCaches && typeof caches !== 'undefined' && caches.keys) {
    try {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    } catch {}
  }
}

/**
 * รีโหลดหน้าอย่างสุภาพ (ให้ service worker/route ทำงานให้เสร็จก่อน)
 * ใช้คู่กับ resetLocalCaches แล้วค่อยเรียก
 */
export function softReload() {
  try {
    if ('requestIdleCallback' in window) {
      window.requestIdleCallback(() => window.location.reload());
    } else {
      setTimeout(() => window.location.reload(), 0);
    }
  } catch {
    // fallback
    window.location.reload();
  }
}

/**
 * ตัวช่วยแบบ one-liner สำหรับเมนูดีบัก:
 * ล้างทุกอย่าง (รวม tokens + IndexedDB + Caches) แล้วรีโหลด
 */
export async function nukeAndReload() {
  await resetLocalCaches({ clearTokens: true, clearIndexedDB: true, clearCaches: true });
  softReload();
}
