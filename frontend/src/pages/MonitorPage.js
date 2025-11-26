// src/pages/MonitorPage.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import "./MonitorPage.css";
import PrinterStatus from "../PrinterStatus";
import VideoStream from "../VideoStream";
import RightPanel from "../RightPanel";
import { useApi } from "../api";

/* ---------- helper ---------- */
const shallowEq = (a, b) => {
  if (a === b) return true;
  if (!a || !b) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  for (const k of ka) {
    if (a[k] !== b[k]) return false;
  }
  return true;
};

/**
 * MonitorPage
 * - SSE: /printers/:id/status/stream  (แนบ ?token= อัตโนมัติผ่าน useApi.sseWithBackoff)
 * - Poll: /printers/:id/octoprint/job   (ใช้ octoprintJobSafe: backoff + cooldown เมื่อ 502)
 * - Fallback: ดึงหัวคิวจาก /printers/:id/current-job แล้ว merge เข้ากับ OctoPrint job
 * - LATENCY: log latency ของ /printers/:id/octoprint/job และ /healthz/live → /latency/log
 */
export default function MonitorPage({
  printerId = process.env.REACT_APP_PRINTER_ID || "prusa-core-one",
}) {
  const api = useApi();

  // ใช้รูป fallback จาก public
  const NO_IMAGE_URL = (process.env.PUBLIC_URL || "") + "/icon/noimage.png";

  // ---- state หลัก ----
  const [printerOnline, setPrinterOnline] = useState(false);
  const [printerStatus, setPrinterStatus] = useState(
    "Offline — waiting for connection"
  );
  const [printState, setPrintState] = useState("offline"); // printing | paused | error | ready | offline | idle

  const [estimatedSeconds, setEstimatedSeconds] = useState(0);
  const [startedAt, setStartedAt] = useState(null);
  const [remainingSeconds, setRemainingSeconds] = useState(null);

  const [currentJob, setCurrentJob] = useState(null); // { name, thumb, durationMin?, startedAt?, completion? }
  const [currentQueueNumber, setCurrentQueueNumber] = useState(null);
  const [material, setMaterial] = useState(null);

  const normalizeState = (s) => {
    const t = String(s || "").toLowerCase();
    if (t.includes("error") || t.includes("fail")) return "error";
    if (t.includes("pause")) return "paused";
    if (t.includes("print")) return "printing";
    if (t.includes("offline")) return "offline";
    if (t.includes("ready") || t.includes("operational")) return "ready";
    return t || "idle";
  };

  // track mount
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ---- SSE: /printers/:id/status/stream ----
  const sseCloserRef = useRef(null);
  const lastSseAtRef = useRef(0);

  // เก็บค่าล่าสุดไว้เทียบ เพื่อลด setState ซ้ำ
  const lastStatusRef = useRef({
    online: null,
    statusText: null,
    state: null,
  });

  useEffect(() => {
    try {
      sseCloserRef.current?.close?.();
    } catch {}
    sseCloserRef.current = null;

    const path = `/printers/${encodeURIComponent(printerId)}/status/stream`;
    const closer = api.sseWithBackoff(path, {
      withToken: true,
      onOpen: () => {
        lastSseAtRef.current = Date.now();
      },
      onMessage: (ev) => {
        if (!ev || typeof ev.data !== "string" || !ev.data.trim()) return;
        try {
          const msg = JSON.parse(ev.data);
          const s = msg?.data || msg;
          const online =
            typeof s?.is_online === "boolean" ? s.is_online : !!s?.online;
          const st = normalizeState(s?.state);
          const statusText =
            s?.status_text ||
            (online ? "Printer is ready" : "Offline — waiting for connection");

          lastSseAtRef.current = Date.now();
          if (!mountedRef.current) return;

          // อัปเดตเฉพาะเมื่อเปลี่ยนจริง
          if (lastStatusRef.current.online !== online) {
            setPrinterOnline(online);
            lastStatusRef.current.online = online;
          }
          if (lastStatusRef.current.state !== st) {
            setPrintState(st);
            lastStatusRef.current.state = st;
          }
          if (lastStatusRef.current.statusText !== statusText) {
            setPrinterStatus(statusText);
            lastStatusRef.current.statusText = statusText;
          }

          if (st !== "printing" && st !== "paused") {
            setRemainingSeconds(null);
            setEstimatedSeconds(0);
            setStartedAt(null);
            setCurrentJob((prev) =>
              prev && prev.completion === 100 ? prev : null
            );
            setMaterial(null);
          }
        } catch {
          /* ignore */
        }
      },
      onError: () => {
        if (!mountedRef.current) return;
        setPrinterOnline(false);
        setPrintState("offline");
        setPrinterStatus("Offline — waiting for connection");
        lastStatusRef.current = {
          online: false,
          state: "offline",
          statusText: "Offline — waiting for connection",
        };
      },
    });
    sseCloserRef.current = closer;

    return () => {
      try {
        closer?.close?.();
      } catch {}
      sseCloserRef.current = null;
    };
  }, [api, printerId]);

  // Watchdog: ถ้าไม่ได้รับสัญญาณ SSE > 15s ให้ถือว่าออฟไลน์ชั่วคราว
  useEffect(() => {
    const t = setInterval(() => {
      const last = lastSseAtRef.current || 0;
      if (Date.now() - last > 15000) {
        if (!mountedRef.current) return;
        setPrinterOnline(false);
        setPrintState("offline");
        setPrinterStatus("Offline — waiting for connection");
        lastStatusRef.current = {
          online: false,
          state: "offline",
          statusText: "Offline — waiting for connection",
        };
      }
    }, 5000);
    return () => clearInterval(t);
  }, []);

  // ---- Poll: /printers/:id/octoprint/job ----
  const lastJobRef = useRef(null);

  useEffect(() => {
    let stop = false;
    let timer = null;

    const chooseIntervalMs = () => {
      if (document.visibilityState === "hidden") return 20000;
      if (printState === "printing") return 3000;
      if (printState === "paused") return 5000;
      if (printState === "ready") return 8000;
      return 10000;
    };

    const scheduleNext = () => {
      if (stop) return;
      const iv = chooseIntervalMs();
      timer = setTimeout(tick, iv);
    };

    const tick = async () => {
      if (stop || document.visibilityState === "hidden") {
        scheduleNext();
        return;
      }

      // === LATENCY: วัดเวลารอบ /octoprint/job ===
      const tSend = Date.now();
      let data = null;

      try {
        data = await api.printer.octoprintJobSafe(printerId, {
          timeoutMs: 12000,
        });
        const tRecv = Date.now();
        const latencyMs = tRecv - tSend;

        // ยิง log ไป backend (ถ้าล้มเหลวไม่ต้องทำอะไรต่อ)
        try {
          await api.post(
            "/latency/log",
            {
              channel: "web",
              path: `/printers/${printerId}/octoprint/job`,
              t_send: new Date(tSend).toISOString(),
              t_recv: new Date(tRecv).toISOString(),
              latency_ms: latencyMs,
              note: `MonitorPage:octoprintJob:${printerId}`,
            },
            { timeoutMs: 5000 }
          );
        } catch {
          /* ignore logging error */
        }

        if (!mountedRef.current) return;
        if (!data) {
          scheduleNext();
          return;
        }

        const job = data?.octoprint?.job || {};
        const progress = data?.octoprint?.progress || {};
        const stateText = String(data?.octoprint?.state || "");
        const mappedState = normalizeState(stateText);
        const mapped = data?.mapped || {};

        // update mapping/online/status (เฉพาะเมื่อเปลี่ยน)
        if (lastStatusRef.current.state !== mappedState) {
          setPrintState(mappedState);
          lastStatusRef.current.state = mappedState;
        }
        if (lastStatusRef.current.online !== true) {
          setPrinterOnline(true);
          lastStatusRef.current.online = true;
        }
        const mappedText =
          mappedState === "paused"
            ? "Paused"
            : mappedState === "printing"
            ? "Printing..."
            : mappedState === "ready"
            ? "Printer is ready"
            : "Offline — waiting for connection";
        if (lastStatusRef.current.statusText !== mappedText) {
          setPrinterStatus(mappedText);
          lastStatusRef.current.statusText = mappedText;
        }

        const fileName =
          job?.file?.name ||
          mapped?.file_name ||
          mapped?.file ||
          null;

        const materialFromOcto =
          job?.file?.material ||
          job?.material ||
          job?.filament?.tool0?.material ||
          job?.filament?.tool1?.material ||
          null;
        if (materialFromOcto) {
          const up = String(materialFromOcto).toUpperCase();
          if (material !== up) setMaterial(up);
        }

        const printTime = Number(progress?.printTime ?? 0);
        const printTimeLeft = Number(progress?.printTimeLeft ?? 0);
        const completion = Number(progress?.completion ?? 0);

        const estimatedFromProgress =
          printTime > 0 && printTimeLeft > 0 ? printTime + printTimeLeft : 0;
        const estimatedTotal =
          estimatedFromProgress || Number(job?.estimatedPrintTime ?? 0) || 0;

        if (estimatedTotal !== estimatedSeconds)
          setEstimatedSeconds(estimatedTotal || 0);

        const nextRemain =
          printTimeLeft > 0
            ? printTimeLeft
            : estimatedTotal > 0
            ? Math.max(0, estimatedTotal - printTime)
            : null;
        if (nextRemain !== remainingSeconds) setRemainingSeconds(nextRemain);

        const nextStarted =
          printTime > 0
            ? new Date(Date.now() - printTime * 1000).toISOString()
            : null;
        if (nextStarted !== startedAt) setStartedAt(nextStarted);

        // ดึง thumb จาก mapping ถ้ามี, ถ้าไม่มีก็ใช้ของเดิมจาก lastJobRef ก่อนค่อย fallback NO_IMAGE
        const thumbFromMapped =
          mapped?.thumb_url ||
          mapped?.thumbnail_url ||
          mapped?.thumb ||
          null;
        const thumb =
          thumbFromMapped ||
          lastJobRef.current?.thumb ||
          NO_IMAGE_URL;

        // queue number จาก mapping (ถ้ามี)
        if (mapped?.queue_number != null) {
          const qn = String(mapped.queue_number).padStart(3, "0");
          if (qn !== currentQueueNumber) {
            setCurrentQueueNumber(qn);
          }
        }

        const nextJob = {
          name: fileName || lastJobRef.current?.name || "File Name",
          thumb,
          durationMin: estimatedTotal
            ? Math.round(estimatedTotal / 60)
            : lastJobRef.current?.durationMin,
          startedAt:
            printTime > 0
              ? new Date(Date.now() - printTime * 1000).toISOString()
              : lastJobRef.current?.startedAt,
          completion,
        };

        if (!shallowEq(lastJobRef.current || {}, nextJob)) {
          setCurrentJob(nextJob);
          lastJobRef.current = nextJob;
        }
      } catch {
        /* silent */
      } finally {
        scheduleNext();
      }
    };

    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [
    api,
    printerId,
    printState,
    NO_IMAGE_URL,
    estimatedSeconds,
    remainingSeconds,
    startedAt,
    material,
    currentQueueNumber,
  ]);

  // ---- Fallback: ดึงงานหัวคิวจาก backend current-job แล้ว merge กับ currentJob ----
  useEffect(() => {
    let stopped = false;

    const tick = async () => {
      if (stopped || !mountedRef.current) return;

      try {
        const res = await api.get(
          `/printers/${encodeURIComponent(printerId)}/current-job`,
          {},
          { timeoutMs: 8000 }
        );
        if (stopped || !mountedRef.current) return;

        if (!res) {
          // ถ้าไม่มีข้อมูลจาก current-job:
          // - ถ้าไม่ได้พิมพ์อยู่ → clear
          // - ถ้ายัง printing/paused → ปล่อยให้ Octo เป็นตัวหลัก
          if (printState !== "printing" && printState !== "paused") {
            setCurrentJob(null);
            setCurrentQueueNumber(null);
            lastJobRef.current = null;
          }
          return;
        }

        // CurrentJobOut จาก backend:
        // { queue_number, file_name, thumbnail_url, job_id, status, started_at, time_min, remaining_min }
        const prev = lastJobRef.current || currentJob || {};

        const mergedJob = {
          // ✅ ให้ความสำคัญชื่อจาก backend ก่อน
          name:
            (res.file_name && res.file_name.trim()) ||
            prev.name ||
            "File Name",
          // รูป: ถ้า current-job มี thumbnail_url ให้ใช้เลย
          thumb:
            res.thumbnail_url ||
            prev.thumb ||
            NO_IMAGE_URL,
          // duration: ถ้ามีจาก Octo แล้วไม่ทับ, ถ้าไม่มีค่อยใช้จาก time_min
          durationMin:
            prev.durationMin ??
            (Number(res.time_min ?? 0) || undefined),
          // startedAt: ถ้ามีจาก Octo แล้วไม่ทับ
          startedAt:
            prev.startedAt ||
            res.started_at ||
            res.startedAt ||
            undefined,
          // completion: ให้ Octo เป็นเจ้าหลัก
          completion: prev.completion,
        };

        if (!shallowEq(prev || {}, mergedJob)) {
          setCurrentJob(mergedJob);
          lastJobRef.current = mergedJob;
        }

        // queue number → แสดง "001", "002", ...
        let qn = currentQueueNumber;
        if (res.queue_number != null) {
          const padded = String(res.queue_number).padStart(3, "0");
          if (padded !== qn) {
            qn = padded;
            setCurrentQueueNumber(padded);
          }
        }
      } catch {
        // ถ้า 404 หรือ error อื่น:
        // - ถ้าไม่ได้พิมพ์อยู่ → clear
        // - ถ้ากำลังพิมพ์ → ปล่อยให้ Octo เป็นตัวหลัก
        if (stopped || !mountedRef.current) return;
        if (printState !== "printing" && printState !== "paused") {
          setCurrentJob(null);
          setCurrentQueueNumber(null);
          lastJobRef.current = null;
        }
      }
    };

    // เรียกครั้งแรกทันที แล้วค่อยวนทุก 8 วินาที
    tick();
    const iv = setInterval(tick, 8000);

    return () => {
      stopped = true;
      clearInterval(iv);
    };
  }, [api, printerId, NO_IMAGE_URL, currentQueueNumber, printState, currentJob]);

  // === LATENCY: health check /healthz/live → /latency/log ===
  useEffect(() => {
    let stopped = false;
    let timer = null;

    const loop = async () => {
      if (stopped) return;

      // ถ้า tab ซ่อนอยู่ → ไม่ต้องถี่ยมาก
      if (document.visibilityState === "hidden") {
        timer = setTimeout(loop, 20000);
        return;
      }

      const tSend = Date.now();
      try {
        await api.get("/healthz/live", {}, { timeoutMs: 8000 });
        const tRecv = Date.now();
        const latencyMs = tRecv - tSend;

        try {
          await api.post(
            "/latency/log",
            {
              channel: "web",
              path: "/healthz/live",
              t_send: new Date(tSend).toISOString(),
              t_recv: new Date(tRecv).toISOString(),
              latency_ms: latencyMs,
              note: `MonitorPage:healthz:${printerId}`,
            },
            { timeoutMs: 5000 }
          );
        } catch {
          /* ignore logging error */
        }
      } catch {
        // เวลา healthz ล้มเหลว ก็ยัง log latency ไว้ได้
        const tRecv = Date.now();
        const latencyMs = tRecv - tSend;
        try {
          await api.post(
            "/latency/log",
            {
              channel: "web",
              path: "/healthz/live",
              t_send: new Date(tSend).toISOString(),
              t_recv: new Date(tRecv).toISOString(),
              latency_ms: latencyMs,
              note: `MonitorPage:healthz:error:${printerId}`,
            },
            { timeoutMs: 5000 }
          );
        } catch {
          /* ignore logging error */
        }
      } finally {
        if (!stopped) {
          timer = setTimeout(loop, 10000); // ทุก ๆ 10 วินาที
        }
      }
    };

    loop();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [api, printerId]);

  // ---- remaining text ----
  const remainingText = useMemo(() => {
    if (remainingSeconds == null) return "-";
    const sec = Math.max(0, Math.round(remainingSeconds));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }, [remainingSeconds]);

  return (
    <div className="monitor-layout">
      <div className="left-col">
        <div className="left-stack">
          <PrinterStatus status={printerStatus} printerOnline={printerOnline} />

          <VideoStream
            estimatedSeconds={estimatedSeconds}
            startedAt={startedAt}
            state={printState}
            job={currentJob}
            queueNumber={currentQueueNumber}
            /* ↓ ลดการกระพริบของ snapshot โดยคุม FPS ที่นี่ */
            targetFps={8}
            minFpsHidden={2}
            objectFit="contain"
          />
        </div>
      </div>

      <div className="right-col">
        <RightPanel
          printerId={printerId}
          remainingTime={remainingText}
          material={material}
        />
      </div>
    </div>
  );
}