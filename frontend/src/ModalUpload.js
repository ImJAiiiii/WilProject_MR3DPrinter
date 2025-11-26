// src/ModalUpload.js
import React, { useRef, useState, useCallback, useEffect, useMemo } from 'react';
import './ModalUpload.css';
import PreviewPrintModal from './PreviewPrintModal';
import { useApi } from './api/index';
import { useAuth } from './auth/AuthContext';

const NAME_REGEX = /^[A-Za-z0-9._-]+_V\d+$/;
const LINE_WIDTH = 0.42; // mm

const SUPPORT_LABEL = {
  none: 'None',
  build_plate_only: 'Support on build plate only',
  enforcers_only: 'For support enforcers only',
  everywhere: 'Everywhere',
};

// ใช้สอดคล้องกับ BE
const MATERIAL_OPTIONS = ['PLA', 'PETG', 'ABS', 'TPU', 'NYLON'];

/* ---------- helpers ---------- */
const getExt = (n = '') => {
  const s = String(n || '');
  const i = s.lastIndexOf('.');
  return i >= 0 ? s.slice(i + 1).toLowerCase() : '';
};
const isGcodeExt = (ext = '') => ['gcode', 'gco', 'gc'].includes((ext || '').toLowerCase());
// ⛔ จำกัด “เมช” เป็น STL เท่านั้น
const isMeshExt  = (ext = '') => ['stl'].includes((ext || '').toLowerCase());
const baseName   = (n = '') => String(n || '').replace(/\.[^.]+$/, '');
const ensureGcodeName = (n = '') =>
  (n || 'model').match(/\.(gcode|gco|gc)$/i) ? n : `${baseName(n)}.gcode`;
const isStagingKey = (k = '') => /^staging\//i.test(k || '');
const isFinalKey   = (k = '') => !!(k && !/^staging\//i.test(k || ''));

// ใช้ทำค้นชื่อคล้าย
const normalizeName = (s = '') =>
  (s || '').trim().toLowerCase().replace(/\s+/g, '_').replace(/_v\d+$/i, '');

// printer slug ที่ BE ใช้
const normalizePrinterId = (x) => {
  const s = String(x || '').trim();
  if (!s) return (process.env.REACT_APP_PRINTER_ID || 'prusa-core-one');
  const slug = s
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/--+/g, '-')
    .replace(/^-+|-+$/g, '');
  if (slug.startsWith('prusa') && slug.includes('core') && slug.includes('one')) {
    return 'prusa-core-one';
  }
  return slug || (process.env.REACT_APP_PRINTER_ID || 'prusa-core-one');
};

// กรองคำแนะนำให้เหลือเฉพาะชื่อที่เป็น G-code หรือ pattern ที่ถูก
const isGcodeName = (n = '') => /\.(gcode|gco|gc)$/i.test(n || '');
const isMeshName  = (n = '') => /\.(stl)$/i.test(n || '');
const allowHint = (n = '') => {
  const s = (n || '').trim();
  if (!s) return false;
  if (isMeshName(s)) return false; // hint เฉพาะ gcode
  if (isGcodeName(s)) return true;
  const stem = s.replace(/\.[^.]+$/, '');
  return NAME_REGEX.test(stem);
};

/* ---------- MODEL helpers (HONTECH / DELTA / Other) ---------- */
const normalizeModel = (m = '') => {
  const up = String(m || '').trim().toUpperCase();
  if (up === 'HONTECH') return 'HONTECH';
  if (up === 'DELTA') return 'DELTA';
  if (up === 'OTHER') return 'OTHER'; // 👈 รองรับ Other
  return null;
};

// โฟลเดอร์สำหรับ S3/MinIO ให้เป็น: catalog/<MODEL>/<BaseName_VN>/
function modelToS3Prefix(model, jobName) {
  const M = normalizeModel(model);
  if (!M) return null;
  const name = String(jobName || '').trim();
  if (!name || !NAME_REGEX.test(name.replace(/\.[^.]+$/,''))) return null; // ต้องผ่านแพทเทิร์นก่อน (ไม่บังคับ .gcode)
  const stem = name.replace(/\.[^.]+$/, ''); // ตัด .gcode ถ้ามี
  return `catalog/${M}/${stem}/`;
}

export default function ModalUpload({
  isOpen,
  onClose,
  onUploaded,
  onQueue,
}) {

  const api = useApi();
  const { token } = useAuth();

  const inputRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);

  // upload state
  const [fileNameRaw, setFileNameRaw] = useState('');
  const [fileId, setFileId] = useState(null);   // staging/*
  const [fileExt, setFileExt] = useState('');
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState('idle'); // idle | uploading | done | error
  const [error, setError] = useState('');

  // form
  const [model, setModel] = useState(''); // 'HONTECH' | 'DELTA' | '__OTHER'
  const [userFileName, setUserFileName] = useState('');
  const [infill, setInfill] = useState(15);
  const [walls, setWalls] = useState(2);
  const [support, setSupport] = useState('none');
  const [wallsMsg, setWallsMsg] = useState('');

  // Filament material (เฉพาะ STL)
  const [material, setMaterial] = useState('PLA');

  // preview
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [confirming, setConfirming] = useState(false);

  // preparing overlay
  const [preparing, setPreparing] = useState(false);

  // กันชื่อซ้ำ + hints
  const [nameLoading, setNameLoading] = useState(false);
  const [nameHints, setNameHints] = useState([]);        // string[]
  const [similarItems, setSimilarItems] = useState([]);  // object[] (optional)
  const [nameExists, setNameExists] = useState(false);
  const [nameError, setNameError] = useState('');
  const [nameFocus, setNameFocus] = useState(false);

  const debouncedQuery = useDebounce(userFileName, 250);
  const queryKey = useMemo(() => normalizeName(debouncedQuery), [debouncedQuery]);

  const isGcode = isGcodeExt(fileExt);
  const isMesh  = isMeshExt(fileExt);

  // ใช้ชื่อโมเดลที่จะเซฟจริง: ถ้าเลือก Other → ใช้ "Other"
  const effectiveModel = useMemo(() => {
    return model === '__OTHER' ? 'OTHER' : model || '';
  }, [model]);

  const openPicker = () => {
    if (preparing || confirming) return;
    inputRef.current?.click();
  };

  /* ---------- upload via presigned (→ staging/*) ---------- */
  const uploadViaPresign = useCallback(async (file) => {
    const ext = getExt(file.name);
    // เดา content-type ให้ตรงกับ BE: STL | G-code เท่านั้น
    const ctype =
      isMeshExt(ext)
        ? 'model/stl'
        : isGcodeExt(ext)
          ? 'text/x.gcode'
          : (file.type || 'application/octet-stream');

    // ❗ เปลี่ยนมาใช้ query string ตามที่ backend ต้องการ
    const params = new URLSearchParams({
      filename: file.name,
      size: String(file.size ?? 0),
      content_type: ctype,
      overwrite: 'false',
    });

    const base = (api.API_BASE || '').replace(/\/+$/,'');
    const url  = `${base}/api/storage/upload/request?${params.toString()}`;

    const res = await fetch(url, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });

    if (!res.ok) {
      const txt = await res.text().catch(()=> '');
      throw new Error(`upload request failed: ${res.status} ${txt || ''}`.trim());
    }

    // รูปแบบตอบกลับที่รองรับ: { url, headers?, object_key } หรือ presign แบบ S3 fields
    const req = await res.json().catch(()=> ({}));
    const putUrl     = req?.url;
    const headers    = req?.headers || {};
    const stagingKey = req?.object_key;

    if (!putUrl || !stagingKey) {
      throw new Error('Bad /api/storage/upload/request response');
    }

    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', putUrl, true);

      Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
      if (!headers['Content-Type'] && ctype) {
        xhr.setRequestHeader('Content-Type', ctype);
      }

      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) setProgress(Math.round((evt.loaded / evt.total) * 100));
      };

      // presigned อาจช้า → ตั้ง 3 นาที
      xhr.timeout = 180000;
      xhr.ontimeout = () => reject(new Error('Upload timeout'));
      xhr.onerror   = () => reject(new Error('Network error while uploading (PUT presigned)'));

      xhr.onload = () => {
        // S3/MinIO: 200/204
        if ([200, 201, 204].includes(xhr.status)) {
          resolve();
        } else if (xhr.status === 403) reject(new Error('403 Forbidden (presign headers mismatch)'));
        else if (xhr.status === 404)   reject(new Error('404 Not Found (presigned URL expired)'));
        else if (xhr.status === 413)   reject(new Error('File too large (HTTP 413)'));
        else reject(new Error(`Upload failed (HTTP ${xhr.status})`));
      };
      xhr.send(file);
    });

    // แจ้ง BE ให้บันทึกเมทาดาต้าหลังอัปโหลดเสร็จ (best effort)
    try {
      await api.storage.completeUpload({
        object_key: stagingKey,
        filename  : file.name,
        content_type: ctype,
        size: file.size,
      });
    } catch {/* เงียบไว้ */}

    return { objectKey: stagingKey };
  }, [api.API_BASE, api.storage, token]);

  /* ---------- fallback: legacy upload ---------- */
  const uploadFallbackLegacy = useCallback(async (file) => {
    const form = new FormData();
    form.append('file', file);
    const res = await new Promise((resolve, reject) => {
      const url = `${api.API_BASE || ''}/api/files/upload`.replace(/\/+$/,'').replace(/\/{2,}/g,'/');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) setProgress(Math.round((evt.loaded / evt.total) * 100));
      };

      xhr.timeout = 180000;
      xhr.ontimeout = () => reject(new Error('Upload timeout'));
      xhr.onerror   = () => reject(new Error('Network error while uploading'));

      xhr.onload = () => {
        try {
          if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText || '{}'));
          else if (xhr.status === 401) reject(new Error('Unauthorized. Please sign in again.'));
          else if (xhr.status === 413) reject(new Error('File too large (HTTP 413)'));
          else reject(new Error(`Upload failed (${xhr.status})`));
        } catch { reject(new Error('Upload failed (bad response)')); }
      };
      xhr.send(form);
    });

    const id = res.fileId || res.file_id || res.id || res.saved_name || null;
    if (!id) throw new Error('Invalid response from /api/files/upload');
    if (!isStagingKey(String(id))) throw new Error('Legacy uploader must return a staging/* key');

    try {
      await api.storage.completeUpload({
        object_key: id,
        filename  : file.name,
        content_type: res.content_type || 'application/octet-stream',
        size: res.size || file.size,
      });
    } catch {}

    return { objectKey: id };
  }, [api.API_BASE, api.storage, token]);

  /* ---------- upload handlers ---------- */
  const handleFile = useCallback(async (file) => {
    if (!file || preparing || confirming) return;

    const ext = getExt(file.name);
    // ⛔ บล็อคสกุลไฟล์ที่ไม่ใช่ STL หรือ G-code
    if (!isMeshExt(ext) && !isGcodeExt(ext)) {
      setError('Only STL and G-code files are supported.');
      setStatus('error');
      return;
    }

    setError('');
    setFileNameRaw(file.name);
    setFileExt(ext);
    setProgress(0);
    setStatus('uploading');

    try {
      let result;
      try {
        result = await uploadViaPresign(file);
      } catch (e) {
        // กรณี presigned ล้มเหลวเพราะสิทธิ์/หมดอายุ → ตกไปใช้ legacy
        if (/403|404|405/i.test(String(e?.message || ''))) {
          result = await uploadFallbackLegacy(file);
        } else {
          throw e;
        }
      }

      const stagingKey = result.objectKey;
      if (!isStagingKey(stagingKey)) {
        throw new Error('Uploaded object_key is not under staging/.');
      }

      setProgress(100);
      setStatus('done');
      setFileId(stagingKey);

      onUploaded?.({
        fileName: file.name,
        fileId: stagingKey,
        ext: ext,
        file: { name: file.name, object_key: stagingKey },
      });

      if (!userFileName) {
        const stem = baseName(file.name);
        // ถ้าเป็น G-code แล้วลงท้าย _Vn อยู่แล้ว ให้คงเดิม ไม่งั้นเติม _V1
        const next = NAME_REGEX.test(stem) ? stem : `${stem}_V1`;
        setUserFileName(next);
      }
    } catch (err) {
      setStatus('error');
      setError(String(err?.message || err || 'Upload failed'));
      // eslint-disable-next-line no-console
      console.error('upload failed:', err);
    }
  }, [onUploaded, uploadViaPresign, uploadFallbackLegacy, userFileName, preparing, confirming]);

  const onInputChange = (e) => {
    const f = e.target.files?.[0];
    handleFile(f);
    e.target.value = '';
  };

  const clearFile = () => {
    if (preparing || confirming) return;
    setFileNameRaw('');
    setFileId(null);
    setFileExt('');
    setProgress(0);
    setStatus('idle');
    setError('');
    setModel('');
    setUserFileName('');
    setInfill(15);
    setWalls(2);
    setSupport('none');
    setWallsMsg('');
    setMaterial('PLA');
    setPreviewOpen(false);
    setPreviewData(null);
    setNameHints([]);
    setSimilarItems([]);
    setNameExists(false);
    setNameError('');
  };

  /* ---------- validation ---------- */
  const nameOk = NAME_REGEX.test((userFileName || '').trim());
  const canPrepare = status === 'done' && nameOk && !!effectiveModel && !nameExists && !preparing;

  const applyInfill = (val) => {
    let n = Number(val);
    if (!Number.isFinite(n)) n = 0;
    n = Math.round(n);
    setInfill(Math.max(0, Math.min(100, n)));
  };

  const applyWalls = (val, fromTyping = false) => {
    let n = Number(val);
    if (!Number.isFinite(n)) n = 0;
    const rounded = Math.round(n);
    const clamped = Math.max(1, Math.min(6, rounded));
    if (fromTyping && (!Number.isInteger(n) || n < 1 || n > 6)) setWallsMsg('ต้องเป็นจำนวนเต็ม 1–6');
    else setWallsMsg('');
    setWalls(clamped);
  };
  const stepWalls = (d) => applyWalls((Number(walls) || 0) + d, false);

  /* ---------- หา “ชื่อคล้าย” + กันชื่อซ้ำ ---------- */
  useEffect(() => {
    let cancelled = false;
    async function run() {
      setNameError('');
      setNameExists(false);
      setNameHints([]);
      setSimilarItems([]);

      const q = (userFileName || '').trim();
      if (!q || q.length < 2) return;

      setNameLoading(true);
      try {
        // 1) ตรวจซ้ำ/รูปแบบ
        const v = await api.post('/api/storage/validate-name', {
          name: q,
          ext: 'gcode',
          require_pattern: true,
        });

        if (!cancelled) {
          if (v?.ok === false && v?.reason === 'duplicate') {
            setNameExists(true);
            setNameError('ชื่อไฟล์นี้มีอยู่แล้ว กรุณาเปลี่ยนชื่อ');
            setNameHints(
              Array.isArray(v?.suggestions)
                ? v.suggestions.filter(allowHint).slice(0, 8)
                : []
            );
          }
        }

        // 2) ชื่อคล้าย
        const base = normalizeName(q);
        if (!cancelled && base) {
          const s = await api.get('/api/storage/search-names', { q: base, limit: 8 }).catch(() => null);
          if (!cancelled && s && Array.isArray(s.items)) {
            const onlyOk = s.items.filter(allowHint);
            setNameHints((prev) => {
              const merged = Array.from(new Set([...(prev || []), ...onlyOk]));
              return merged.slice(0, 8);
            });
          }
        }
      } catch {
        // เงียบ ๆ
      } finally {
        !cancelled && setNameLoading(false);
      }
    }
    run();
    return () => { cancelled = true; };
  }, [api, queryKey, userFileName]);

  const onPickHint = (name) => {
    setUserFileName(name);
    // ชื่อจาก hint ส่วนใหญ่เป็นชื่อที่เคยใช้ → แจ้งเตือนซ้ำไว้เลย
    setNameExists(true);
    setNameError('ชื่อไฟล์นี้มีอยู่แล้ว กรุณาเปลี่ยนชื่อ');
  };

  /* ---------- Prepare -> Slicer preview ---------- */
  const handlePrepare = async () => {
    if (!canPrepare || !fileId) return;
    if (!isStagingKey(fileId)) {
      setError('The selected file is not in staging/. Please re-upload.');
      return;
    }

    // STL เท่านั้นที่มี slicing params; ถ้าเป็น G-code จะไม่ส่ง slicing
    const materialMaybe = !isGcode ? material : undefined;

    // ส่ง prefix โครงใหม่: catalog/<MODEL>/<BaseName_VN>/
    const s3_prefix = modelToS3Prefix(effectiveModel, (userFileName || '').trim());

    const payload = {
      fileId,
      originExt: fileExt,
      jobName: (userFileName || '').trim(),
      model: effectiveModel, // << ใช้ "Other" เมื่อเลือก Other
      slicing: isGcode ? null : {
        infill: Number(infill),
        walls: Number(walls),
        support,
        ...(materialMaybe ? { material: materialMaybe } : {}),
      },
      ...(s3_prefix ? { s3_prefix } : {}),
    };

    try {
      setPreparing(true);
      const data = await api.post('/api/slicer/preview', payload, undefined, { timeout: 60000, retries: 0 });

      const gkFromApi =
        data.gcodeKey || data.gcode_key || data.gcodeId ||
        data?.gcode?.key || data?.gcode?.object_key ||
        data?.output?.gcode_key || data?.output?.key || null;

      const gk = gkFromApi || (isGcode ? fileId : null);

      // presign GET URL (best-effort)
      let gu = null;
      if (gk) {
        try {
          const pres = await api.storage.presignGet(gk, false);
          gu = pres?.url || null;
        } catch { gu = null; }
      }

      setPreviewData({
        snapshotUrl: data.snapshotUrl || data.preview_image_url || null, // (จะถูกแทนด้วยรูปจาก WebGL ตอน Confirm)
        printer: data.printer,
        settings: {
          infill : data.settings?.infill  ?? (isGcode ? 15 : Number(infill)),
          walls  : data.settings?.walls   ?? (isGcode ? 2  : Number(walls)),
          support: data.settings?.support ?? (isGcode ? 'none' : support),
          ...(materialMaybe ? { material: data.settings?.material ?? materialMaybe } : {}),
          model: effectiveModel, // << ติด model ไว้ใน preview ด้วย
          name: (userFileName || '').trim(),
        },
        result: data.result || null,
        gcodeKey: gk || null,
        gcodeUrl: gu || null,
        originalFileId: fileId,
        isGcode,
        isMesh,
      });
      setPreviewOpen(true);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(err);
      setError(`Failed to call slicer/preview: ${err?.message || 'Unknown error'}`);
    } finally {
      setPreparing(false);
    }
  };

  /* ---------- Confirm Print ---------- */
  const handleConfirmPrint = async (payloadFromPreview) => {
    try {
      if (confirming) return;
      setConfirming(true);

      let gk =
        payloadFromPreview?.gcode_key ||
        payloadFromPreview?.gcodeKey ||
        previewData?.gcodeKey ||
        (isGcode ? fileId : null);

      if (!gk) {
        setConfirming(false);
        throw new Error('Missing G-code key from preview.');
      }

      let finalGcodeKey = gk;

      // staging/* → ต้อง finalize
      if (isStagingKey(gk)) {
        const finalName = ensureGcodeName((userFileName || fileNameRaw || 'model.gcode').trim());
        try {
          const fin = await api.storage.finalize({
            object_key: gk,
            filename  : finalName,
            content_type: 'text/x.gcode',
            model: effectiveModel, // << ให้ BE สร้างใต้ catalog/Other/... ได้
          });
          finalGcodeKey = fin?.object_key || fin?.gcode_key;
          if (!finalGcodeKey) throw new Error('missing object_key');
        } catch (e) {
          const msg = String(e?.message || '');
          if (/409|duplicate/i.test(msg)) throw new Error('ชื่อนี้ถูกใช้แล้ว (409). เปลี่ยนชื่อ File Name แล้วกด Confirm อีกครั้ง');
          if (/413|too large/i.test(msg)) throw new Error('ไฟล์ใหญ่เกินกำหนด (413).');
          if (/422/i.test(msg))           throw new Error('อนุญาตเฉพาะไฟล์ G-code เท่านั้น (422).');
          throw new Error('Finalize failed: ' + msg);
        }
      } else if (!isFinalKey(gk)) {
        setConfirming(false);
        throw new Error('Unsupported object_key prefix.');
      }

      const materialMaybe = !isGcode ? material : undefined;

      // พก snapshot จาก WebGL (data URL) ไปด้วย
      const snapshotDataUrl =
        payloadFromPreview?.thumb_data_url ||
        payloadFromPreview?.snapshotUrl ||
        null;

      const printPayload = {
        ...payloadFromPreview,               // มี thumb_data_url / snapshotUrl อยู่แล้ว
        source: 'upload',
        gcode_key   : finalGcodeKey,        // ไม่ใช่ staging/*
        original_key: isMesh ? (payloadFromPreview?.original_key || null) : (fileId || null),
        name: (userFileName || '').trim() || fileNameRaw || 'Unnamed',
        ...(materialMaybe ? { material: materialMaybe } : {}),
      };

      const printerIdRaw =
        previewData?.printer?.id ||
        previewData?.printer ||
        process.env.REACT_APP_PRINTER_ID ||
        'prusa-core-one';
      const printerId = normalizePrinterId(printerIdRaw);

      const job = await api.post('/api/print', printPayload, { printer_id: printerId }, { timeout: 30000, retries: 0 });

      // ส่งข้อมูลกลับให้หน้าหลัก (พร้อมรูปจาก WebGL เพื่อโชว์ทันที)
      const timeMin   = printPayload?.time_min  ?? previewData?.result?.time_min ?? previewData?.result?.timeMin ?? null;
      const timeText  = printPayload?.time_text ?? previewData?.result?.time_text ?? null;
      const filamentG = printPayload?.filament_g ?? previewData?.result?.filament_g ?? previewData?.result?.filamentG ?? null;

      const settings = {
        model   : previewData?.settings?.model ?? effectiveModel ?? null,
        printer : printerId,
        infill  : previewData?.settings?.infill ?? Number(infill),
        walls   : previewData?.settings?.walls ?? Number(walls),
        support : previewData?.settings?.support ?? support,
        ...(materialMaybe ? { material: previewData?.settings?.material ?? materialMaybe } : {}),
        name    : (userFileName || '').trim() || fileNameRaw || 'Unnamed',
      };
      const supportMode = settings.support || 'none';
      const supportText = SUPPORT_LABEL[supportMode] || supportMode;

      onUploaded?.({
        name : settings.name,
        // ใส่ภาพจาก WebGL เพื่อให้คิวโชว์รูปทันที
        thumb: snapshotDataUrl || null,
        stats: { timeMin, time_text: timeText, filament_g: filamentG },
        settings,
        template: {
          model  : settings.model,
          printer: settings.printer,
          infill : Number(settings.infill),
          infill_percent: Number(settings.infill),
          infillPercent : Number(settings.infill),
          sparse_infill_density: Number(settings.infill),
          walls: Number(settings.walls),
          wall_loops: Number(settings.walls),
          wallLoops : Number(settings.walls),
          support: supportText,
          support_mode: supportMode,
          supportMode : supportMode,
          support_text: supportText,
          ...(materialMaybe ? { material: settings.material, filament: settings.material, filament_material: settings.material } : {}),
          timeMin: timeMin ?? undefined,
          time_text: timeText ?? undefined,
          filament_g: filamentG ?? undefined,
        },
        gcode_key   : finalGcodeKey,
        gcode_path  : null,
        original_key: isMesh ? (printPayload?.original_key || null) : (fileId || null),
        ext: fileExt,
        file: { name: settings.name, object_key: isMesh ? (printPayload?.original_key || null) : (fileId || null) },
      });

      setConfirming(false);
      setPreviewOpen(false);
      onClose?.();
      clearFile();
      onQueue?.();

      return job;
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(err);
      setConfirming(false);

      const msg = String(err?.message || err || 'Unknown error');
      if (/UNIQUE constraint failed: storage_files\.name_low/i.test(msg) || /duplicate/i.test(msg)) {
        setError('ชื่อไฟล์ G-code นี้ถูกใช้ไปแล้วในระบบ (ซ้ำ). กรุณาเปลี่ยนชื่อ File Name แล้วลองใหม่');
      } else if (/Finalize failed/i.test(msg)) {
        setError('Finalize ไม่สำเร็จ: object_key ไม่ถูกต้อง หรือหมดอายุ กรุณาอัปโหลดใหม่');
      } else if (/printer.*not.*found/i.test(msg)) {
        setError('ไม่พบเครื่องพิมพ์ที่เลือก กรุณาเลือกเครื่องพิมพ์ใหม่อีกครั้ง');
      } else if (/413|too large/i.test(msg)) {
        setError('ไฟล์ใหญ่เกินกำหนด (413).');
      } else if (/422/i.test(msg)) {
        setError('อนุญาตเฉพาะไฟล์ G-code เท่านั้น (422).');
      } else {
        setError(`Failed to confirm print: ${msg}`);
      }
      throw err;
    }
  };

  /* ---------- Key สำหรับ Preview ---------- */
  const previewKey =
    (previewData?.gcodeKey || previewData?.gcodeUrl || '') +
    (previewOpen ? ':open' : ':closed');

  // เลือก “รายการที่ใกล้เคียงสุด” (optional)
  const topSimilar = useMemo(() => {
    if (!similarItems?.length) return null;
    const q = (userFileName || '').trim().toLowerCase();
    const pickName = (x) => (x?.name || x?.file_name || x?.filename || x?.original_name || '').toLowerCase();
    return similarItems
      .map(x => ({ x, score: scoreSimilarity(pickName(x), q) }))
      .sort((a, b) => b.score - a.score)[0]?.x || null;
  }, [similarItems, userFileName]);

  // ข้อความ error ใต้ช่องชื่อไฟล์
  const nameErrorMsg = (() => {
    if (nameError) return nameError;
    const invalidByRegex = !!(userFileName && !NAME_REGEX.test((userFileName || '').trim()));
    return invalidByRegex ? 'Name must be like ModelName_V1' : '';
  })();

  if (!isOpen) return null;
  
  return (
    <>
      {/* ซ่อนกล่องอัปโหลดเมื่อพรีวิวเปิด */}
      {!previewOpen && (
        <div className="modal-overlay" onClick={() => { if (!preparing && !confirming) onClose?.(); }}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <button className="close-btn" onClick={() => { if (!preparing && !confirming) onClose?.(); }} aria-label="Close">
              <img src={process.env.PUBLIC_URL + '/icon/Close.png'} alt="" className="close-icon" />
            </button>

            <div className="upload-box">
              <div
                className={`upload-area ${dragOver ? 'is-dragover' : ''}`}
                onDragOver={(e) => { e.preventDefault(); if (!preparing && !confirming) setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                  if (!preparing && !confirming) handleFile(e.dataTransfer.files?.[0]);
                }}
              >
                <img src={process.env.PUBLIC_URL + '/icon/Upload.png'} alt="" className="upload-icon" />
                <div className="upload-text">
                  <strong>Choose a file or drag &amp; drop it here</strong>
                  <p>Supported formats: STL, G-code</p>
                </div>
                <button className="browse-btn" onClick={openPicker} disabled={preparing || confirming}>
                  {preparing ? 'Preparing…' : 'Browse File'}
                </button>
                <input
                  ref={inputRef}
                  type="file"
                  // ⛔ จำกัดเฉพาะ .stl และ .gcode/.gco/.gc
                  accept=".stl,.gcode,.gco,.gc"
                  className="file-input-hidden"
                  onChange={onInputChange}
                  disabled={preparing || confirming}
                />
              </div>

              {(status === 'uploading' || status === 'done' || status === 'error') && (
                <div className="upload-row">
                  <div className="row-head">
                    <span className={`tag ${status}`}>
                      {status === 'uploading' ? 'Uploading' : status === 'done' ? 'Uploaded' : 'Error'}
                    </span>
                    <button className="row-clear" onClick={clearFile} aria-label="Remove file" disabled={preparing || confirming}>×</button>
                  </div>

                  <div className="row-file">
                    <span className="file-name" title={fileNameRaw} aria-label={fileNameRaw}>
                      {fileNameRaw || '—'}
                    </span>
                    {status === 'uploading' && <span className="file-pct">{progress}%</span>}
                  </div>

                  <div className="progress-bar">
                    <div className="progress-fill" style={{ width: `${progress}%` }} />
                  </div>

                  {error && <div className="err-msg">{error}</div>}
                </div>
              )}

              {status === 'done' && (
                <div className="slice-form">
                  {/* Model */}
                  <div className="form-row">
                    <label className="form-label">
                      <span className="label-main">Model</span>
                      <span className="req">*</span>
                    </label>
                    <select
                      className="input select"
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                      required
                      disabled={preparing || confirming}
                    >
                      <option value="" disabled hidden>Select</option>
                      <option value="HONTECH">HONTECH</option>
                      <option value="DELTA">DELTA</option>
                      <option value="__OTHER">OTHER</option>
                    </select>
                  </div>

                  {/* File name */}
                  <div className="form-row">
                    <label className="form-label -inline">
                      <span className="label-main">File Name</span>
                      <span className="req">*</span>
                      <span className="help">ModelName_Version (eg. ModelName_V1)</span>
                    </label>

                    <div className={`namebox ${nameExists ? 'has-error' : ''}`} style={{ position:'relative' }}>
                      <input
                        className={`input ${(userFileName && !NAME_REGEX.test((userFileName || '').trim())) || !!nameError ? 'invalid' : ''}`}
                        placeholder="ModelName_V1"
                        value={userFileName}
                        onChange={(e) => setUserFileName(e.target.value)}
                        autoComplete="off"
                        onFocus={() => setNameFocus(true)}
                        onBlur={() => setTimeout(() => setNameFocus(false), 150)}
                        disabled={preparing || confirming}
                      />
                      {nameLoading && <div className="namebox-spinner" aria-hidden />}
                      {(nameFocus && userFileName.trim().length >= 2 && !!nameHints.length && !nameExists) && (
                        <ul className="namebox-hints" role="listbox">
                          {nameHints.map((n) => (
                            <li
                              key={n}
                              role="option"
                              onMouseDown={(e) => { e.preventDefault(); onPickHint(n); }}
                            >
                              {n}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>

                    {/* error text */}
                    {nameErrorMsg && (
                      <div style={{ color: '#b00020', fontSize: 12, marginTop: 6 }}>
                        {nameErrorMsg === 'Name must be like ModelName_V1'
                          ? <>Name must be like <b>ModelName_V1</b></>
                          : nameErrorMsg}
                      </div>
                    )}
                  </div>

                  {/* Filament material — เฉพาะ STL */}
                  {!isGcode && (
                    <div className="form-row">
                      <label className="form-label">
                        <span className="label-main">Filament material</span>
                        <span className="req">*</span>
                      </label>
                      <select
                        className="input select"
                        value={material}
                        onChange={(e) => setMaterial(e.target.value)}
                        required
                        disabled={preparing || confirming}
                      >
                        {MATERIAL_OPTIONS.map((m) => (
                          <option key={m} value={m}>{m}</option>
                        ))}
                      </select>
                    </div>
                  )}

                  {/* Slice params (hide when G-code) */}
                  {!isGcode && (
                    <div className="controls-row -two">
                      <div className="form-row">
                        <label className="form-label">
                          <span className="label-main">Sparse infill density</span>
                          <span className="req">*</span>
                        </label>
                        <input
                          type="number"
                          inputMode="numeric"
                          pattern="[0-9]*"
                          className="input"
                          min={0}
                          max={100}
                          step={1}
                          value={infill}
                          onChange={(e) => applyInfill(e.target.value)}
                          onKeyDown={(e) => { if (['e','E','+','-','.'].includes(e.key)) e.preventDefault(); }}
                          title="0–100%"
                          disabled={preparing || confirming}
                        />
                      </div>

                      <div className="form-row walls-row">
                        <label className="form-label">
                          <span className="label-main">Wall loops</span>
                          <span className="req">*</span>
                        </label>
                        <div className="stepper">
                          <button type="button" className="step-btn -minus" onClick={() => stepWalls(-1)} disabled={walls <= 1 || preparing || confirming}>−</button>
                          <input
                            type="number"
                            inputMode="numeric"
                            pattern="[0-9]*"
                            className="input step-input"
                            min={1}
                            max={6}
                            step={1}
                            value={walls}
                            onChange={(e) => applyWalls(e.target.value, true)}
                            onBlur={(e) => applyWalls(e.target.value, true)}
                            onKeyDown={(e) => { if (['e','E','+','-','.'].includes(e.key)) e.preventDefault(); }}
                            title="1–6"
                            aria-describedby="wallAssist"
                            disabled={preparing || confirming}
                          />
                          <button type="button" className="step-btn -plus" onClick={() => stepWalls(1)} disabled={walls >= 6 || preparing || confirming}>+</button>
                        </div>
                        <div className="assist" id="wallAssist" aria-live="polite">
                          <div className="calc">
                            ความหนาผนัง: {walls} × {LINE_WIDTH.toFixed(2)} = {(walls * LINE_WIDTH).toFixed(2)} mm
                          </div>
                          {wallsMsg && <div className="warn">{wallsMsg}</div>}
                        </div>
                      </div>
                    </div>
                  )}

                  {!isGcode && (
                    <div className="form-row">
                      <label className="form-label">
                        <span className="label-main">Support</span>
                      </label>
                      <select className="input select" value={support} onChange={(e) => setSupport(e.target.value)} disabled={preparing || confirming}>
                        <option value="none">None</option>
                        <option value="build_plate_only">Support on build plate only</option>
                        <option value="enforcers_only">For support enforcers only</option>
                        <option value="everywhere">Everywhere</option>
                      </select>
                    </div>
                  )}
                </div>
              )}

              <button
                className={`prepare-btn ${canPrepare ? 'is-primary' : ''}`}
                disabled={!canPrepare}
                onClick={handlePrepare}
              >
                {preparing ? 'Preparing…' : 'Prepare to print'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Preparing overlay */}
      {preparing && (
        <div className="prep-scrim" role="alert" aria-live="assertive" onClick={(e)=>e.stopPropagation()}>
          <div className="prep-card -busy" onClick={(e)=>e.stopPropagation()}>
            <div className="prep-icon" aria-hidden>
              <svg className="prep-spinner" viewBox="0 0 24 24">
                <circle className="track" cx="12" cy="12" r="9" />
                <circle className="indicator" cx="12" cy="12" r="9" />
              </svg>
            </div>

            <div className="prep-title">
              {isGcode ? 'Opening G-code preview' : 'Converting STL to G-code'}
            </div>
            <div className="prep-sub">
              {isGcode
                ? 'Loading preview and printer settings…'
                : 'Slicing your model. This can take a moment for complex parts.'}
            </div>

            <div className="prep-bar" aria-hidden>
              <div className="prep-bar-fill" />
            </div>
          </div>
        </div>
      )}

      {/* Preview */}
      {previewOpen && previewData && (
        <PreviewPrintModal
          key={previewKey}
          open={true}
          onClose={() => setPreviewOpen(false)}
          data={previewData}
          onConfirm={handleConfirmPrint}
          confirming={confirming}
        />
      )}
    </>
  );
}

/* ---------- utilities ---------- */
function useDebounce(value, delay = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return v;
}

function scoreSimilarity(nameLower = '', qLower = '') {
  if (!nameLower || !qLower) return 0;
  if (nameLower === qLower) return 100;
  if (nameLower.startsWith(qLower)) return 90;
  if (nameLower.includes(qLower)) return 75;
  const overlap = lcsLen(nameLower, qLower);
  return Math.round(50 * (overlap / Math.max(1, qLower.length)));
}

function lcsLen(a, b) {
  const m = a.length, n = b.length;
  const dp = new Array(n + 1).fill(0);
  for (let i = 1; i <= m; i++) {
    let prev = 0;
    for (let j = 1; j <= n; j++) {
      const tmp = dp[j];
      dp[j] = a[i - 1] === b[j - 1] ? prev + 1 : Math.max(dp[j], dp[j - 1]);
      prev = tmp;
    }
  }
  return dp[n];
}
