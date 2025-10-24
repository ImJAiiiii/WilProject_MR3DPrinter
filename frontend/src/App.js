// src/App.js
import React, { useEffect, useMemo, useRef, useState } from 'react';
import './App.css';
import Navbar from './Navbar';
import ModalUpload from './ModalUpload';

import MonitorPage from './pages/MonitorPage';
import PrintingPage from './pages/PrintingPage';
import StoragePage from './pages/StoragePage';

import Login from './Login';
import { useAuth } from './auth/AuthContext';
import { useApi } from './api/index';

const LS_STORAGE = 'customStorage';
const LS_HISTORY = 'userHistory';
const PRINTER_ID = process.env.REACT_APP_PRINTER_ID || 'prusa-core-one';
const IMG_3D = process.env.PUBLIC_URL + '/images/3D.png';

// ---------- polling/timeout tuning ----------
const QUEUE_POLL_MS = 7000;
const QUEUE_MAX_BACKOFF = 30000;
const TIMEOUT_QUEUE = 20000;
const TIMEOUT_CREATE = 60000;
const TIMEOUT_CANCEL = 20000;

// ---------- local storage helpers ----------
const readJSON = (k, d) => {
  try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; }
  catch { return d; }
};
const writeJSON = (k, v) => {
  try { localStorage.setItem(k, JSON.stringify(v)); }
  catch { /* ignore quota */ }
};

// ---------- parsing helpers ----------
function parseTimeTextToMin(text) {
  if (!text) return null;
  // “1h 13m”, “1 h 13 m”, “75m”, “1h”
  const hm = /^\s*(\d+)\s*h(?:\s*(\d+)\s*m)?\s*$/i.exec(text);
  if (hm) return (+hm[1]) * 60 + (+hm[2] || 0);
  const m = /^\s*(\d+)\s*m\s*$/i.exec(text);
  if (m) return +m[1];
  return null;
}
function looksLikeGcode(nameOrExt) {
  const s = String(nameOrExt || '').toLowerCase();
  return /\.g(code|co|c)$/.test(s) || ['gcode', 'gco', 'gc'].includes(s.replace(/^\./, ''));
}

// ---------- stats normalizer ----------
function normalizeStats(stats) {
  if (!stats) return null;
  const out = { ...stats };
  if (out.timeMin == null) {
    const mm = parseTimeTextToMin(out.time_text);
    if (mm != null) out.timeMin = mm;
  } else {
    out.timeMin = Number(out.timeMin);
  }
  if (out.filament_g != null) out.filament_g = Number(out.filament_g);
  return out;
}

// ---------- helpers for history/storage item (FE unified) ----------
function toItem(payload) {
  const name =
    payload?.name ||
    payload?.file?.name ||
    payload?.fileName ||
    payload?.template?.model ||
    'Unnamed';

  const objectKey =
    payload?.file?.object_key ||
    payload?.fileId ||
    payload?.object_key ||
    null;

  const gcodeKey = payload?.gcode_key ?? payload?.gcodeId ?? null;
  const gcodePath =
    payload?.gcode_path ||
    payload?.gcodeUrl ||
    (payload?.gcodeId ? `/uploads/${payload.gcodeId}` : null);

  const isGcode = looksLikeGcode(name) || looksLikeGcode(payload?.ext);

  const thumb =
    payload?.thumb ||
    payload?.snapshotUrl ||
    payload?.preview_image_url ||
    payload?.file?.thumb ||
    payload?.template?.preview ||
    IMG_3D;

  // ดึงเวลาประมาณให้ครบ (รองรับ field จาก slicer)
  const stats =
    normalizeStats(
      payload?.stats ||
        (payload?.time_min != null ? { timeMin: payload.time_min } : null) ||
        (payload?.result?.estimate_min != null
          ? { timeMin: payload.result.estimate_min, time_text: payload?.result?.total_text }
          : null)
    ) || null;

  // เก็บ settings ให้มากสุดเท่าที่มี
  const settings =
    payload?.settings
      ? { ...payload.settings }
      : payload?.template?.settings
      ? { ...payload.template.settings }
      : null;

  return {
    // ✅ preserve server id if present (ใช้กันซ้ำตอน merge)
    _serverId: payload?._serverId ?? null,

    id: Date.now() + Math.floor(Math.random() * 1000), // ให้ไม่ชนกันแน่ๆ
    name,
    thumb,
    stats,
    settings,
    template: payload?.template || null,

    gcode_key: isGcode ? (gcodeKey ?? objectKey ?? null) : (gcodeKey ?? null),
    gcode_path: gcodePath || null,

    // เก็บ original เฉพาะที่ไม่ใช่ gcode (หรือถ้า FE ส่งมาก็เก็บไว้เพื่อ dedupe)
    original_key: !isGcode
      ? (payload?.original_key ?? payload?.file?.object_key ?? objectKey ?? payload?.object_key ?? null)
      : (payload?.original_key ?? payload?.file?.object_key ?? payload?.object_key ?? null),

    uploadedAt: payload?.uploadedAt ?? Date.now(),
  };
}

// ---------- “upsert” : รวม/อัปเดตรายการเดิม ----------
function upsertList(list, payload) {
  const incoming = toItem(payload);

  // 0) ถ้าไอเท็มจาก server มีไอดีจริง ให้ใช้ _serverId จับคู่ก่อนเสมอ
  if (incoming._serverId != null) {
    const pos = list.findIndex((it) => it && it._serverId === incoming._serverId);
    if (pos >= 0) {
      const base = list[pos];
      const mergedSettings = { ...(base.settings || {}), ...(incoming.settings || {}) };
      const normalizedStats =
        normalizeStats({ ...(base.stats || {}), ...(incoming.stats || {}) }) ||
        base.stats ||
        null;

      const merged = {
        ...base,
        name: incoming.name || base.name,
        thumb: incoming.thumb || base.thumb,
        template: incoming.template || base.template,
        settings: Object.keys(mergedSettings).length ? mergedSettings : (base.settings || null),
        stats: normalizedStats,
        gcode_key: incoming.gcode_key || base.gcode_key || null,
        gcode_path: incoming.gcode_path || base.gcode_path || null,
        original_key: incoming.original_key || base.original_key || null,
        uploadedAt: base.uploadedAt || incoming.uploadedAt || Date.now(),
        _serverId: base._serverId ?? incoming._serverId,
      };

      const next = [...list];
      next[pos] = merged;
      const [updated] = next.splice(pos, 1);
      return [updated, ...next];
    }
  }

  // 1) ตรงกับ key เดิมใดๆ → ถือเป็นรายการเดียวกัน
  const candKeys = new Set([
    incoming.original_key || '',
    incoming.gcode_key || '',
    incoming.gcode_path || '',
  ]);
  let idx = list.findIndex((it) => {
    if (!it) return false;
    return (
      candKeys.has(it.original_key || '') ||
      candKeys.has(it.gcode_key || '') ||
      candKeys.has(it.gcode_path || '')
    );
  });

  // 2) ไม่มีคีย์ชัดเจน → fallback heuristic (ชื่อเหมือน + เวลาใกล้กันมาก)
  if (idx === -1) {
    const NEAR_MS = 20 * 1000; // ✅ แคบลงจาก 2 นาทีเหลือ 20 วินาที
    idx = list.findIndex((it) => {
      if (!it) return false;
      const a = it.uploadedAt || 0;
      const b = incoming.uploadedAt || 0;
      const near = Math.abs(a - b) <= NEAR_MS; // ✅ เทียบกันสองรายการ ไม่ใช่กับ now
      return near && it.name && incoming.name && it.name === incoming.name;
    });
  }

  if (idx === -1) {
    return [{ ...incoming }, ...list];
  }

  const base = list[idx];

  const mergedSettings = { ...(base.settings || {}), ...(incoming.settings || {}) };
  const normalizedStats =
    normalizeStats({ ...(base.stats || {}), ...(incoming.stats || {}) }) ||
    base.stats ||
    null;

  const merged = {
    ...base,
    name: incoming.name || base.name,
    thumb: incoming.thumb || base.thumb,
    template: incoming.template || base.template,
    settings: Object.keys(mergedSettings).length ? mergedSettings : (base.settings || null),
    stats: normalizedStats,
    gcode_key: incoming.gcode_key || base.gcode_key || null,
    gcode_path: incoming.gcode_path || base.gcode_path || null,
    original_key: incoming.original_key || base.original_key || null,
    uploadedAt: base.uploadedAt || incoming.uploadedAt || Date.now(),
    _serverId: base._serverId ?? incoming._serverId ?? null,
  };

  const next = [...list];
  next[idx] = merged;
  const [updated] = next.splice(idx, 1);
  return [updated, ...next];
}

// ---------- map Server → FE payload ----------
function fromServerHistoryItem(j) {
  // j: PrintJobOut (backend/schemas.PrintJobOut)
  return {
    _serverId: j.id,                // ✅ ใช้แยกให้ชัดว่าเป็นรายการไหนจากเซิร์ฟเวอร์
    id: j.id,
    name: j.name,
    thumb: j.thumb || IMG_3D,
    stats: {
      timeMin: j.time_min ?? j?.stats?.time_min ?? null,
      time_text: j?.stats?.time_text ?? null,
      filament_g: j?.stats?.filament_g ?? null,
    },
    template: j?.template || null,
    file: {
      name: j?.file?.filename || j.name,
      thumb: j?.file?.thumb || j.thumb || undefined,
      object_key: j?.file?.object_key || undefined, // ถ้ามี
    },
    uploadedAt: j.finished_at ? new Date(j.finished_at).getTime()
              : j.uploaded_at ? new Date(j.uploaded_at).getTime()
              : Date.now(),
    gcode_key: j?.file?.object_key || undefined,
    original_key: undefined,
  };
}

function fromServerStorageFile(sf) {
  // sf: StorageFileOut
  return {
    name: sf.filename,
    file: { name: sf.filename, object_key: sf.object_key, thumb: undefined },
    original_key: sf.object_key,
    thumb: IMG_3D,
    uploadedAt: sf.uploaded_at ? new Date(sf.uploaded_at).getTime() : Date.now(),
  };
}

// body สำหรับ POST /printers/{id}/queue
function toPrintBody(payloadOrItem, source) {
  const name =
    payloadOrItem?.file?.name ||
    payloadOrItem?.name ||
    payloadOrItem?.template?.model ||
    'Unnamed';

  const thumb =
    payloadOrItem?.file?.thumb ||
    payloadOrItem?.thumb ||
    payloadOrItem?.template?.preview ||
    undefined;

  const minutes =
    payloadOrItem?.stats?.timeMin ??
    payloadOrItem?.template?.timeMin ??
    parseTimeTextToMin(payloadOrItem?.stats?.time_text) ??
    0;

  const gcodeKey =
    payloadOrItem?.gcode_key ?? payloadOrItem?.gcodeKey ?? payloadOrItem?.gcodeId ?? null;

  const gcodePath =
    payloadOrItem?.gcode_path ??
    (payloadOrItem?.gcodeId ? `/uploads/${payloadOrItem.gcodeId}` : undefined);

  const originalKey =
    payloadOrItem?.original_key ??
    payloadOrItem?.file?.object_key ??
    payloadOrItem?.object_key ??
    undefined;

  const body = { name, thumb, time_min: minutes, source };
  if (gcodeKey) body.gcode_key = gcodeKey;
  if (!gcodeKey && gcodePath) body.gcode_path = gcodePath;

  const isDupOriginal = originalKey && (originalKey === gcodeKey || originalKey === gcodePath);
  if (originalKey && !isDupOriginal) body.original_key = originalKey;

  return body;
}

// ใช้ตอน push ประวัติฝั่ง FE → BE (idempotent merge)
function toServerHistoryMergeItem(localItem) {
  return {
    name: localItem.name || localItem?.file?.name || 'Unnamed',
    thumb: localItem.thumb || localItem?.file?.thumb || undefined,
    time_min: localItem?.stats?.timeMin ?? parseTimeTextToMin(localItem?.stats?.time_text) ?? null,
    source: 'upload',
    gcode_key: localItem?.gcode_key || localItem?.file?.object_key || null,
    gcode_path: localItem?.gcode_path || null,
    original_key: localItem?.original_key || null,
    template: localItem?.template || null,
    stats: {
      time_min: localItem?.stats?.timeMin ?? null,
      time_text: localItem?.stats?.time_text ?? null,
      filament_g: localItem?.stats?.filament_g ?? null,
    },
    file: localItem?.file ? {
      filename: localItem?.file?.name || localItem.name,
      thumb: localItem?.file?.thumb || localItem.thumb || undefined,
      object_key: localItem?.file?.object_key || localItem?.gcode_key || undefined,
    } : undefined,
  };
}

// map งานจาก backend → รูปแบบที่หน้า Printing ใช้
function mapServerJob(j) {
  return {
    id: j.id,
    name: j.name,
    thumb: j.thumb || IMG_3D,
    durationMin: j.time_min ?? 0,
    status: j.status,
    startedAt: j.started_at ? new Date(j.started_at).getTime() : null,
    uploadedBy: j.employee_id,
    ownerName: j.employee_name ?? null,
    me_can_cancel: j.me_can_cancel ?? false,
    remainingMin: j.remaining_min ?? j.remainingMin ?? null,
    waitBeforeMin: j.wait_before_min ?? j.waitBeforeMin ?? null,
    waitTotalMin: j.wait_total_min ?? j.waitTotalMin ?? null,
  };
}

// คำนวณ waitMin ของแต่ละงาน
function computeWaitMap(jobs) {
  const allHaveServerWait = jobs.length > 0 && jobs.every((j) => j.waitBeforeMin != null);
  if (allHaveServerWait) {
    const map = new Map();
    for (const j of jobs) map.set(j.id, j.waitBeforeMin);
    return map;
  }
  const now = Date.now();
  let accMin = 0;
  const map = new Map();
  for (const j of jobs) {
    map.set(j.id, accMin);
    if (j.status === 'processing') {
      if (j.remainingMin != null) {
        accMin += Math.max(0, j.remainingMin);
      } else {
        const total = j.durationMin || 0;
        const elapsed = j.startedAt ? Math.max(0, Math.floor((now - j.startedAt) / 60000)) : 0;
        const rem = Math.max(0, total - elapsed);
        accMin += rem;
      }
    } else {
      accMin += j.durationMin || 0;
    }
  }
  return map;
}

// ===================== App: เลือกหน้าจอ =====================
export default function App() {
  const { user } = useAuth();
  if (!user) return <Login />;
  return <AuthedApp user={user} />;
}

// ===================== ส่วนที่ล็อกอินแล้ว =====================
function AuthedApp({ user }) {
  const { token, logout } = useAuth();
  const api = useApi();

  const userId = user?.employee_id || user?.id || user?.email || 'anon';

  // ====== Custom Storage (ของกลาง) ======
  const [storageItems, setStorageItems] = useState(() => readJSON(LS_STORAGE, []));
  const [historyMap, setHistoryMap] = useState(() => readJSON(LS_HISTORY, {}));
  const userHistory = historyMap[userId] || [];

  // sync ข้ามแท็บ (และกันโดน “เซฟทับ”)
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === LS_STORAGE) setStorageItems(readJSON(LS_STORAGE, []));
      if (e.key === LS_HISTORY) setHistoryMap(readJSON(LS_HISTORY, {}));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  // ====== Load & merge จาก Server (History + Storage) ======
  useEffect(() => {
    if (!token) return;

    // ---- HISTORY ----
    (async () => {
      try {
        const server = await api.history.listMine({ timeout: 20000 });
        if (Array.isArray(server)) {
          let merged = [...userHistory];
          for (const j of server) {
            merged = upsertList(merged, fromServerHistoryItem(j));
          }
          setHistoryMap(prev => {
            const next = { ...prev, [userId]: merged };
            writeJSON(LS_HISTORY, next);
            return next;
          });
        }
      } catch (e) {
        console.debug('history.listMine failed:', e?.message || e);
      }
    })();

    // ---- STORAGE ----
    (async () => {
      try {
        const server = await api.storage.listAll({ timeout: 20000 });
        if (Array.isArray(server)) {
          let merged = [...storageItems];
          for (const sf of server) {
            merged = upsertList(merged, fromServerStorageFile(sf));
          }
          setStorageItems(merged);
          writeJSON(LS_STORAGE, merged);
        }
      } catch (e) {
        console.debug('storage.listAll failed:', e?.message || e);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, userId]);

  // ====== แจ้งเตือน ======
  const bellLimiterRef = useRef(new Map());
  const notifyBell = async (title, message, { severity = 'info', type = 'app', dedupeKey = '', limitMs = 60000 } = {}) => {
    try {
      if (dedupeKey) {
        const last = bellLimiterRef.current.get(dedupeKey) || 0;
        if (Date.now() - last < limitMs) return;
        bellLimiterRef.current.set(dedupeKey, Date.now());
      }
      await api.post('/notifications', { type, title, message, severity });
    } catch (err) {
      console.debug('notifyBell failed:', err);
    }
  };

  // ====== คิวจาก backend ======
  const [printJobs, setPrintJobs] = useState([]);

  const inflightRef = useRef(false);
  const failCountRef = useRef(0);
  const pollTimerRef = useRef(null);
  const pausedRef = useRef(false);

  const clearTimer = () => { if (pollTimerRef.current) clearTimeout(pollTimerRef.current); pollTimerRef.current = null; };
  const scheduleNext = (ms) => { clearTimer(); pollTimerRef.current = setTimeout(fetchQueue, ms); };

  const fetchQueue = async () => {
    if (!token || pausedRef.current || inflightRef.current) return;
    inflightRef.current = true;
    try {
      const data = await api.queue.list(PRINTER_ID, false, { timeout: TIMEOUT_QUEUE });
      setPrintJobs((data?.items || []).map(mapServerJob));
      failCountRef.current = 0;
      scheduleNext(QUEUE_POLL_MS);
    } catch (err) {
      console.error('fetchQueue failed:', err);
      notifyBell('ไม่สามารถโหลดคิวพิมพ์ได้', String(err?.message || 'Network error'), {
        severity: 'warning',
        type: 'queue',
        dedupeKey: 'fetchQueue-failed',
        limitMs: 60000,
      });
      failCountRef.current += 1;
      const backoff = Math.min(QUEUE_POLL_MS * Math.pow(2, failCountRef.current), QUEUE_MAX_BACKOFF);
      scheduleNext(backoff);
    } finally {
      inflightRef.current = false;
    }
  };

  useEffect(() => {
    const onVis = () => {
      pausedRef.current = document.hidden || !navigator.onLine;
      if (!pausedRef.current) fetchQueue();
    };
    const onOnline = () => { pausedRef.current = false; fetchQueue(); };
    const onOffline = () => { pausedRef.current = true; clearTimer(); };

    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);

    if (token) fetchQueue();

    return () => {
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      clearTimer();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // กำลังพิมพ์ + เวลาเหลือ
  const processing = useMemo(
    () => printJobs.find((j) => j.status === 'processing') || null,
    [printJobs]
  );
  const remainingSeconds = useMemo(() => {
    if (!processing) return null;
    if (processing.remainingMin != null) return Math.max(0, processing.remainingMin) * 60;
    if (!processing.durationMin || !processing.startedAt) return null;
    const elapsed = Math.max(0, Math.floor((Date.now() - processing.startedAt) / 1000));
    return Math.max(0, processing.durationMin * 60 - elapsed);
  }, [processing]);

  // แจ้งเมื่อถึงคิว
  const lastProcIdRef = useRef(null);
  useEffect(() => {
    const isMine = processing && processing.uploadedBy === userId;
    if (isMine && lastProcIdRef.current !== processing?.id) {
      notifyBell('ถึงคิวพิมพ์ของคุณแล้ว', processing?.name || 'Your print job has started.', {
        severity: 'success',
        type: 'job',
        dedupeKey: `job-started-${processing?.id}`,
        limitMs: 24 * 60 * 60 * 1000,
      });
    }
    lastProcIdRef.current = processing ? processing.id : null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processing?.id, processing?.uploadedBy, userId]);

  // ===== เมนู/แอ็กชัน =====
  const [selectedMenu, setSelectedMenu] = useState('Monitor and Control');

  const queueItem = async (item, source) => {
    try {
      const body = toPrintBody(item, source);
      if (!body.gcode_key && !body.gcode_path) {
        notifyBell('ส่งเข้าคิวไม่สำเร็จ', 'ไม่พบ G-code สำหรับงานนี้', {
          severity: 'error',
          type: 'queue',
          dedupeKey: `no-gcode-${item?.id || item?.name || 'x'}`,
          limitMs: 15000,
        });
        return;
      }

      const res = await api.queue.create(body, PRINTER_ID, { timeout: TIMEOUT_CREATE });
      const jobId = res?.id || res?.job_id || res?.job?.id || res?.item?.id;
      if (jobId) sessionStorage.setItem('lastQueuedJobId', String(jobId));

      await fetchQueue();
      setSelectedMenu('Printing');
    } catch (err) {
      console.error('queueItem failed:', err);
      notifyBell('ส่งเข้าคิวไม่สำเร็จ', String(err?.message || 'Network error'), {
        severity: 'error',
        type: 'queue',
        dedupeKey: 'queueItem-failed',
        limitMs: 60000,
      });
    }
  };
  const queueFromStorage = (item) => queueItem(item, 'storage');
  const queueFromHistory = (item) => queueItem(item, 'history');

  // UI state
  const [printerStatus] = useState('Printer is ready');
  const [printerOnline] = useState(true);
  const [showUploadModal, setShowUploadModal] = useState(false);

  // Wait map
  const waitMap = useMemo(() => computeWaitMap(printJobs), [printJobs]);

  const renderPage = () => {
    switch (selectedMenu) {
      case 'Monitor and Control':
        return (
          <MonitorPage
            printerStatus={printerStatus}
            printerOnline={printerOnline}
            currentJob={processing}
            currentQueueNumber={
              processing ? String(printJobs.findIndex((j) => j.id === processing.id) + 1).padStart(3, '0') : null
            }
            remainingSeconds={remainingSeconds}
          />
        );
      case 'Printing':
        return (
          <PrintingPage
            jobs={printJobs}
            currentUserId={userId}
            currentRemainingSeconds={remainingSeconds}
            waitMap={waitMap}
            lastQueuedJobId={sessionStorage.getItem('lastQueuedJobId') || null}
            onCancelJob={async (id) => {
              try {
                await api.queue.cancel(PRINTER_ID, id, { timeout: TIMEOUT_CANCEL });
                setPrintJobs((prev) => prev.filter((j) => j.id !== id));
              } catch (err) {
                const code = err?.status;
                const msg = String(err?.message || 'Network error');
                if (code === 409) {
                  setPrintJobs((prev) => prev.filter((j) => j.id !== id));
                  console.debug('cancel: 409 (already finalized).', msg);
                } else if (code === 403) {
                  notifyBell('ยกเลิกไม่ได้', msg, {
                    severity: 'warning',
                    type: 'queue',
                    dedupeKey: `cancel-403-${id}`,
                    limitMs: 15000,
                  });
                } else {
                  console.error('cancel failed:', err);
                  notifyBell('ยกเลิกงานไม่สำเร็จ', msg, {
                    severity: 'error',
                    type: 'queue',
                    dedupeKey: `cancel-failed-${id}`,
                    limitMs: 15000,
                  });
                }
              } finally {
                await fetchQueue();
              }
            }}
            onOpenUpload={() => setShowUploadModal(true)}
            gotoStorage={() => setSelectedMenu('Custom Storage')}
            onQueueFromHistory={queueFromHistory}
          />
        );
      case 'Custom Storage':
        return <StoragePage items={storageItems} onQueue={(item) => queueFromStorage(item)} />;
      default:
        return null;
    }
  };

  return (
    <div className="App">
      <Navbar
        onUploadClick={() => setShowUploadModal(true)}
        onLogout={logout}
        user={user}
        onOpenPrinting={() => setSelectedMenu('Printing')}
      />

      <div className="main-content">
        <div className="left-box">
          <div
            className={`menu-item ${selectedMenu === 'Monitor and Control' ? 'selected' : ''}`}
            onClick={() => setSelectedMenu('Monitor and Control')}
          >
            <img
              src={
                process.env.PUBLIC_URL +
                (selectedMenu === 'Monitor and Control' ? '/icon/MonitorandControlblue.png' : '/icon/MonitorandControlwhite.png')
              }
              alt="Monitor and Control"
              className="menu-icon"
            />
            Monitor and Control
          </div>

          <div className={`menu-item ${selectedMenu === 'Printing' ? 'selected' : ''}`} onClick={() => setSelectedMenu('Printing')}>
            <img
              src={
                process.env.PUBLIC_URL +
                (selectedMenu === 'Printing' ? '/icon/Printingblue.png' : '/icon/Printingwhite.png')
              }
              alt="Printing"
              className="menu-icon"
            />
            Printing
          </div>

          <div
            className={`menu-item ${selectedMenu === 'Custom Storage' ? 'selected' : ''}`}
            onClick={() => setSelectedMenu('Custom Storage')}
          >
            <img
              src={
                process.env.PUBLIC_URL +
                (selectedMenu === 'Custom Storage' ? '/icon/CustomStorageblue.png' : '/icon/CustomStoragewhite.png')
              }
              alt="Custom Storage"
              className="menu-icon"
            />
            Custom Storage
          </div>
        </div>

        <div className="content-area">{renderPage()}</div>
      </div>

      <ModalUpload
        isOpen={showUploadModal}
        onClose={() => setShowUploadModal(false)}
        onUploaded={async (payload) => {
          // upsert ทั้ง storage กับ history (ตาม user)
          const newStorage = upsertList(storageItems, payload);
          const newHistory = upsertList(userHistory, payload);

          // เขียนลง localStorage แบบ merge map เดิมก่อนทุกครั้ง (กัน “เซฟทับ”)
          const latestStorage = readJSON(LS_STORAGE, []);
          const latestMap = readJSON(LS_HISTORY, {});
          writeJSON(LS_STORAGE, newStorage.length >= latestStorage.length ? newStorage : latestStorage);
          writeJSON(LS_HISTORY, { ...latestMap, [userId]: newHistory });

          setStorageItems(readJSON(LS_STORAGE, newStorage));
          setHistoryMap((prev) => ({ ...prev, [userId]: newHistory }));

          // เก็บไอดีล่าสุด (ของ job ฝั่ง FE) เผื่อใช้ highlight
          const just = newHistory[0];
          if (just?.id != null) sessionStorage.setItem('lastQueuedJobId', String(just.id));

          // ✅ พยายาม sync ประวัติขึ้น server (idempotent)
          try {
            const mergeItem = toServerHistoryMergeItem(toItem(payload));
            await api.history.merge([mergeItem], { timeout: 15000 });
          } catch (e) {
            console.debug('history.merge skipped:', e?.message || e);
          }
        }}
      />
    </div>
  );
}
