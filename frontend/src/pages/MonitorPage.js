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
 * - Fallback: ดึงเวลาจาก BE /api/printers/:id/current-job ถ้า OctoPrint ไม่ให้เวลา
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
    } catch {
      /* ignore */
    }
    sseCloserRef.current = null;

    const path = `/printers/${encodeURIComponent(
      printerId
    )}/status/stream`;
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
            typeof s?.is_online === "boolean"
              ? s.is_online
              : !!s?.online;
          const st = normalizeState(s?.state);
          const statusText =
            s?.status_text ||
            (online
              ? "Printer is ready"
              : "Offline — waiting for connection");

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
          /* ignore parse error */
        }
      },
      onError: () => {
        // ❗ อย่าบังคับ Offline ทันทีที่ SSE error
        // ให้รอ OctoPrint poll หรือ status จริงเป็นคนบอกแทน
        lastSseAtRef.current = 0;
      },
    });
    sseCloserRef.current = closer;

    return () => {
      try {
        closer?.close?.();
      } catch {
        /* ignore */
      }
      sseCloserRef.current = null;
    };
  }, [api, printerId]);

  // (ลบ Watchdog เดิมที่บังคับ Offline เมื่อไม่มี SSE > 15s)
  // เพราะจะทำให้สถานะกระพริบ offline/online เวลา connection ล่มชั่วคราว

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
      try {
        const data = await api.printer.octoprintJobSafe(printerId, {
          timeoutMs: 12000,
        });
        if (!mountedRef.current) return;
        if (!data) {
          scheduleNext();
          return;
        }

        const job = data?.octoprint?.job || {};
        const progress = data?.octoprint?.progress || {};
        const stateText = String(data?.octoprint?.state || "");
        const mapped = normalizeState(stateText);

        const isOnline = mapped !== "offline";

        // update mapping/online/status (เฉพาะเมื่อเปลี่ยน)
        if (lastStatusRef.current.state !== mapped) {
          setPrintState(mapped);
          lastStatusRef.current.state = mapped;
        }
        if (lastStatusRef.current.online !== isOnline) {
          setPrinterOnline(isOnline);
          lastStatusRef.current.online = isOnline;
        }

        const mappedText =
          mapped === "paused"
            ? "Paused"
            : mapped === "printing"
            ? "Printing..."
            : mapped === "ready"
            ? "Printer is ready"
            : mapped === "error"
            ? "Error"
            : "Offline — waiting for connection";

        if (lastStatusRef.current.statusText !== mappedText) {
          setPrinterStatus(mappedText);
          lastStatusRef.current.statusText = mappedText;
        }

        const fileName = job?.file?.name || null;

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
          printTime > 0 && printTimeLeft > 0
            ? printTime + printTimeLeft
            : 0;
        const estimatedTotal =
          estimatedFromProgress ||
          Number(job?.estimatedPrintTime ?? 0) ||
          0;

        if (estimatedTotal !== estimatedSeconds)
          setEstimatedSeconds(estimatedTotal || 0);

        const nextRemain =
          printTimeLeft > 0
            ? printTimeLeft
            : estimatedTotal > 0
            ? Math.max(0, estimatedTotal - printTime)
            : null;
        if (nextRemain !== remainingSeconds)
          setRemainingSeconds(nextRemain);

        const nextStarted =
          printTime > 0
            ? new Date(
                Date.now() - printTime * 1000
              ).toISOString()
            : null;
        if (nextStarted !== startedAt) setStartedAt(nextStarted);

        const nextJob = {
          name: fileName || "File Name",
          thumb: NO_IMAGE_URL,
          durationMin: estimatedTotal
            ? Math.round(estimatedTotal / 60)
            : undefined,
          startedAt:
            printTime > 0
              ? new Date(
                  Date.now() - printTime * 1000
                ).toISOString()
              : undefined,
          completion,
        };
        if (!shallowEq(lastJobRef.current || {}, nextJob)) {
          setCurrentJob(nextJob);
          lastJobRef.current = nextJob;
        }
        // setCurrentQueueNumber(..) // หากมีจาก BE อื่น
      } catch {
        // ถ้าเรียก OctoPrint ไม่ได้ ให้ใช้สถานะล่าสุดต่อ ไม่บังคับ offline ทันที
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
  ]);

  // ---- Fallback current-job ----
  useEffect(() => {
    let aborted = false;

    async function runFallback() {
      const noEstimate = !(estimatedSeconds > 0);
      const noStart = !startedAt;
      if (printState !== "printing" || (!noEstimate && !noStart)) return;

      try {
        const cj = await api.queue.current(printerId, {
          timeout: 8000,
        });
        if (aborted || !mountedRef.current) return;

        const tm = Number(
          cj?.time_min ?? cj?.timeMin ?? 0
        );
        const stIso = cj?.started_at ?? cj?.startedAt ?? null;
        const remMin =
          cj?.remaining_min ?? cj?.remainingMin ?? null;

        const mat =
          cj?.material ||
          cj?.template?.material ||
          cj?.manifest?.material ||
          (Array.isArray(cj?.filaments) &&
            cj.filaments[0]?.material) ||
          null;
        if (mat) {
          const up = String(mat).toUpperCase();
          if (material !== up) setMaterial(up);
        }

        if (tm > 0 && noEstimate) setEstimatedSeconds(tm * 60);
        if (stIso && noStart) {
          const next =
            typeof stIso === "number"
              ? new Date(stIso).toISOString()
              : stIso;
          setStartedAt(next);
        }

        if (remMin != null) {
          setRemainingSeconds(
            Math.max(0, Math.round(remMin * 60))
          );
        } else if (tm > 0 && stIso) {
          const elapsed = Math.max(
            0,
            Math.floor(
              (Date.now() -
                new Date(stIso).getTime()) /
                1000
            )
          );
          setRemainingSeconds(
            Math.max(0, tm * 60 - elapsed)
          );
        }

        const nextJob = {
          name:
            cj?.file_name ||
            lastJobRef.current?.name ||
            "File Name",
          thumb:
            cj?.thumbnail_url ||
            lastJobRef.current?.thumb ||
            NO_IMAGE_URL,
          durationMin:
            tm || lastJobRef.current?.durationMin,
          startedAt:
            stIso || lastJobRef.current?.startedAt,
          completion: lastJobRef.current?.completion,
        };
        if (!shallowEq(lastJobRef.current || {}, nextJob)) {
          setCurrentJob(nextJob);
          lastJobRef.current = nextJob;
        }
        if (cj?.queue_number != null) {
          const qn = String(cj.queue_number).padStart(3, "0");
          if (qn !== currentQueueNumber)
            setCurrentQueueNumber(qn);
        }
      } catch {
        /* silent */
      }
    }

    runFallback();
    return () => {
      aborted = true;
    };
  }, [
    api,
    printerId,
    printState,
    estimatedSeconds,
    startedAt,
    NO_IMAGE_URL,
    currentQueueNumber,
    material,
  ]);

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
          <PrinterStatus
            status={printerStatus}
            printerOnline={printerOnline}
          />

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
