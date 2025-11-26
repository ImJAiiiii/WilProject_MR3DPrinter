// src/pages/MonitorPage.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import "./MonitorPage.css";
import PrinterStatus from "../PrinterStatus";
import VideoStream from "../VideoStream";
import RightPanel from "../RightPanel";
import { useApi } from "../api";
import { useAuth } from "../auth/AuthContext";

const NO_IMAGE_URL = "/icon/noimage.png";

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

  // ✅ ใช้รูป fallback ภายในไฟล์นี้ (ไม่สร้างไฟล์ใหม่)
  const NO_IMAGE_URL = "/icon/noimage.png";

  // ---- state หลัก ----
  // เริ่มต้นให้รีล: ยังไม่เชื่อม → ออฟไลน์/รอเชื่อมต่อ
  const [printerOnline, setPrinterOnline] = useState(false);
<<<<<<< Updated upstream
  const [printerStatus, setPrinterStatus] = useState("Offline — waiting for connection"); // status_text
=======
  const [printerStatus, setPrinterStatus] = useState(
    "Offline — waiting for connection"
  );
>>>>>>> Stashed changes
  const [printState, setPrintState] = useState("offline"); // printing | paused | error | ready | offline | idle

  const [estimatedSeconds, setEstimatedSeconds] = useState(0);     // เวลารวมที่จะพิมพ์เสร็จ (วินาที)
  const [startedAt, setStartedAt] = useState(null);                // ISO string | null
  const [remainingSeconds, setRemainingSeconds] = useState(null);  // วินาที | null

  const [currentJob, setCurrentJob] = useState(null);              // { name, thumb, durationMin?, startedAt?, completion?, employee_id? }
  const [currentQueueNumber, setCurrentQueueNumber] = useState(null);
  const [currentOwnerEmp, setCurrentOwnerEmp] = useState(null);    // employee_id เจ้าของงาน

  // material ของงานปัจจุบัน (จาก OctoPrint/BE)
  const [material, setMaterial] = useState(null);

  // ---- helper แปลง state เป็นกลุ่ม ----
  const normalizeState = (s) => {
    const t = String(s || "").toLowerCase();
    if (t.includes("error") || t.includes("fail")) return "error";
    if (t.includes("pause")) return "paused";
    if (t.includes("print")) return "printing";
    if (t.includes("offline")) return "offline";
    if (t.includes("ready") || t.includes("operational")) return "ready";
    return t || "idle";
  };

  // track mount ป้องกัน setState หลัง unmount
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ---- SSE: /printers/:id/status/stream → online/state/status_text/progress ----
  const sseCloserRef = useRef(null);
  const lastSseAtRef = useRef(0);

  useEffect(() => {
<<<<<<< Updated upstream
    // ปิด connection เก่าก่อน
    try { sseCloserRef.current?.close?.(); } catch {}
=======
    try {
      sseCloserRef.current?.close?.();
<<<<<<< HEAD
    } catch {}
=======
    } catch {
      /* ignore */
    }
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
>>>>>>> Stashed changes
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
          // backend ส่ง { type: "status", data: PrinterStatusOut }
          const s = msg?.data || msg;
<<<<<<< Updated upstream
          const online = typeof s?.is_online === "boolean" ? s.is_online : !!s?.online;
=======
          const online =
<<<<<<< HEAD
            typeof s?.is_online === "boolean" ? s.is_online : !!s?.online;
          const st = normalizeState(s?.state);
          const statusText =
            s?.status_text ||
            (online ? "Printer is ready" : "Offline — waiting for connection");
=======
            typeof s?.is_online === "boolean"
              ? s.is_online
              : !!s?.online;
          const st = normalizeState(s?.state);
          const statusText =
            s?.status_text ||
            (online
              ? "Printer is ready"
              : "Offline — waiting for connection");
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2

          lastSseAtRef.current = Date.now();
>>>>>>> Stashed changes
          if (!mountedRef.current) return;

          lastSseAtRef.current = Date.now();
          setPrinterOnline(online);
          const st = normalizeState(s?.state);
          setPrintState(st);
          setPrinterStatus(s?.status_text || (online ? "Printer is ready" : "Offline — waiting for connection"));

          // เมื่อไม่อยู่ในโหมดพิมพ์ เคลียร์ค่าที่อาจหลงเหลือ
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
<<<<<<< Updated upstream
          // ignore malformed payload
=======
          /* ignore parse error */
>>>>>>> Stashed changes
        }
      },
      onError: () => {
<<<<<<< HEAD
        if (!mountedRef.current) return;
        setPrinterOnline(false);
        setPrintState("offline");
        setPrinterStatus("Offline — waiting for connection");
<<<<<<< Updated upstream
=======
        lastStatusRef.current = {
          online: false,
          state: "offline",
          statusText: "Offline — waiting for connection",
        };
=======
        // ❗ อย่าบังคับ Offline ทันทีที่ SSE error
        // ให้รอ OctoPrint poll หรือ status จริงเป็นคนบอกแทน
        lastSseAtRef.current = 0;
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
>>>>>>> Stashed changes
      },
    });
    sseCloserRef.current = closer;

    return () => {
      try {
        closer?.close?.();
<<<<<<< HEAD
      } catch {}
=======
      } catch {
        /* ignore */
      }
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
      sseCloserRef.current = null;
    };
  }, [api, printerId]);

<<<<<<< HEAD
  // Watchdog: ถ้าไม่ได้รับสัญญาณ SSE > 15s ให้ถือว่าออฟไลน์ชั่วคราว
  useEffect(() => {
    const t = setInterval(() => {
      const last = lastSseAtRef.current || 0;
      if (Date.now() - last > 15000) {
        if (!mountedRef.current) return;
        setPrinterOnline(false);
        setPrintState("offline");
        setPrinterStatus("Offline — waiting for connection");
<<<<<<< Updated upstream
=======
        lastStatusRef.current = {
          online: false,
          state: "offline",
          statusText: "Offline — waiting for connection",
        };
>>>>>>> Stashed changes
      }
    }, 5000);
    return () => clearInterval(t);
  }, []);
=======
  // (ลบ Watchdog เดิมที่บังคับ Offline เมื่อไม่มี SSE > 15s)
  // เพราะจะทำให้สถานะกระพริบ offline/online เวลา connection ล่มชั่วคราว
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2

  // ---- Poll: /printers/:id/octoprint/job → เวลาที่เหลือ/ไฟล์ ฯลฯ ----
  // ใช้รุ่น Safe (มี cooldown 60s เมื่อเจอ 502)
  useEffect(() => {
    let stop = false;
    let timer = null;

    const chooseIntervalMs = () => {
      // เร็วตอนกำลังพิมพ์, ช้าลงตอนว่าง/ออฟไลน์ เพื่อลดโหลด
      if (document.visibilityState === "hidden") return 20000;
      if (printState === "printing") return 3000;
      if (printState === "paused") return 5000;
      if (printState === "ready") return 8000;
      return 10000; // offline/error/idle
    };

    const scheduleNext = () => {
      if (stop) return;
      const iv = chooseIntervalMs();
      timer = setTimeout(tick, iv);
    };

    const tick = async () => {
      if (stop) return;

      // ประหยัดโหลดเมื่อแท็บถูกซ่อน: ข้ามบางรอบ (คง state ล่าสุดไว้)
      if (document.visibilityState === "hidden") {
        scheduleNext();
        return;
      }

<<<<<<< Updated upstream
      try {
        // ใช้ octoprintJobSafe ซึ่งจะทำ backoff + ตั้ง cooldown ให้เองเมื่อเจอ 502
        const data = await api.printer.octoprintJobSafe(printerId, { timeoutMs: 12000 });
        if (!mountedRef.current) return;
        if (!data) { // อยู่ช่วง cooldown → ยังไม่อัปเดตอะไร ปล่อยให้รอบหน้า
=======
      // === LATENCY: วัดเวลารอบ /octoprint/job ===
      const tSend = Date.now();
      let data = null;

      try {
<<<<<<< HEAD
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

=======
        const data = await api.printer.octoprintJobSafe(printerId, {
          timeoutMs: 12000,
        });
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
        if (!mountedRef.current) return;
        if (!data) {
>>>>>>> Stashed changes
          scheduleNext();
          return;
        }

        // payload: { octoprint: { job, progress }, ... }
        const job = data?.octoprint?.job || {};
        const progress = data?.octoprint?.progress || {};
        const stateText = String(data?.octoprint?.state || "");
<<<<<<< Updated upstream
        const mapped = normalizeState(stateText);
        setPrintState(mapped);
        setPrinterOnline(true);
        setPrinterStatus(
          mapped === "paused"   ? "Paused" :
          mapped === "printing" ? "Printing..." :
          mapped === "ready"    ? "Printer is ready" :
        "Offline — waiting for connection"
        );
        const fileName = job?.file?.name || null;
=======
        const mappedState = normalizeState(stateText);
        const mapped = data?.mapped || {};

        const isOnline = mapped !== "offline";

        // update mapping/online/status (เฉพาะเมื่อเปลี่ยน)
        if (lastStatusRef.current.state !== mappedState) {
          setPrintState(mappedState);
          lastStatusRef.current.state = mappedState;
        }
        if (lastStatusRef.current.online !== isOnline) {
          setPrinterOnline(isOnline);
          lastStatusRef.current.online = isOnline;
        }

        const mappedText =
<<<<<<< HEAD
          mappedState === "paused"
            ? "Paused"
            : mappedState === "printing"
            ? "Printing..."
            : mappedState === "ready"
            ? "Printer is ready"
            : "Offline — waiting for connection";
=======
          mapped === "paused"
            ? "Paused"
            : mapped === "printing"
            ? "Printing..."
            : mapped === "ready"
            ? "Printer is ready"
            : mapped === "error"
            ? "Error"
            : "Offline — waiting for connection";

>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
        if (lastStatusRef.current.statusText !== mappedText) {
          setPrinterStatus(mappedText);
          lastStatusRef.current.statusText = mappedText;
        }

        const fileName =
          job?.file?.name ||
          mapped?.file_name ||
          mapped?.file ||
          null;
>>>>>>> Stashed changes

        // เดา material จาก OctoPrint (ถ้า plugin/โครงสร้างรองรับ)
        const materialFromOcto =
          job?.file?.material ||
          job?.material ||
          job?.filament?.tool0?.material ||
          job?.filament?.tool1?.material ||
          null;
        if (materialFromOcto) {
          setMaterial(String(materialFromOcto).toUpperCase());
        }

        const printTime = Number(progress?.printTime ?? 0);         // วินาทีที่พิมพ์ไปแล้ว
        const printTimeLeft = Number(progress?.printTimeLeft ?? 0); // วินาทีที่เหลือ
        const completion = Number(progress?.completion ?? 0);       // %

        const estimatedFromProgress =
          printTime > 0 && printTimeLeft > 0
            ? printTime + printTimeLeft
            : 0;
        const estimatedTotal =
          estimatedFromProgress ||
          Number(job?.estimatedPrintTime ?? 0) ||
          0;

<<<<<<< Updated upstream
        setEstimatedSeconds(estimatedTotal || 0);
        setRemainingSeconds(
=======
        if (estimatedTotal !== estimatedSeconds)
          setEstimatedSeconds(estimatedTotal || 0);

        const nextRemain =
>>>>>>> Stashed changes
          printTimeLeft > 0
            ? printTimeLeft
            : estimatedTotal > 0
            ? Math.max(0, estimatedTotal - printTime)
<<<<<<< Updated upstream
            : null
        );
        setStartedAt(
          printTime > 0
            ? new Date(Date.now() - printTime * 1000).toISOString()
            : null
        );

        // ✅ ใช้รูป fallback เสมอเมื่อไม่รู้ URL ภาพ
        setCurrentJob({
=======
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
<<<<<<< HEAD
          name: fileName || lastJobRef.current?.name || "File Name",
          thumb,
          durationMin: estimatedTotal
            ? Math.round(estimatedTotal / 60)
            : lastJobRef.current?.durationMin,
          startedAt:
            printTime > 0
              ? new Date(Date.now() - printTime * 1000).toISOString()
              : lastJobRef.current?.startedAt,
=======
>>>>>>> Stashed changes
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
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
          completion,
<<<<<<< Updated upstream
          employee_id: currentOwnerEmp || null, // ยังไม่รู้จาก Octo → รอ fallback เติม
        });

        // ถ้ามี queue number ที่ backend อื่นส่งมา สามารถเซ็ตได้ที่นี่:
        // setCurrentQueueNumber(...)
      } catch (e) {
        // เงียบไว้ (safe variant จะตั้ง cooldown เองหากเป็น 502)
=======
        };

        if (!shallowEq(lastJobRef.current || {}, nextJob)) {
          setCurrentJob(nextJob);
          lastJobRef.current = nextJob;
        }
      } catch {
        // ถ้าเรียก OctoPrint ไม่ได้ ให้ใช้สถานะล่าสุดต่อ ไม่บังคับ offline ทันที
>>>>>>> Stashed changes
      } finally {
        scheduleNext();
      }
    };

    // เริ่มทำงาน
    tick();

    // เมื่อ component ถูกถอด → ยกเลิกรอบถัดไป
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
<<<<<<< Updated upstream
  }, [api, printerId, printState, currentOwnerEmp]);

  // ---- Fallback: ถ้า OctoPrint ไม่ส่งเวลา ให้ใช้ข้อมูลจาก BE current-job ----
=======
  }, [
    api,
    printerId,
    printState,
    NO_IMAGE_URL,
    estimatedSeconds,
    remainingSeconds,
    startedAt,
    material,
<<<<<<< HEAD
    currentQueueNumber,
=======
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
  ]);

  // ---- Fallback: ดึงงานหัวคิวจาก backend current-job แล้ว merge กับ currentJob ----
>>>>>>> Stashed changes
  useEffect(() => {
    let stopped = false;

<<<<<<< Updated upstream
    async function runFallback() {
      // เงื่อนไขใช้ fallback เฉพาะตอนกำลังพิมพ์ และไม่มีเวลาที่เชื่อถือได้
      const noEstimate = !(estimatedSeconds > 0);
      const noStart = !startedAt;
      if (printState !== "printing" || (!noEstimate && !noStart)) return;
=======
    const tick = async () => {
      if (stopped || !mountedRef.current) return;
>>>>>>> Stashed changes

      try {
<<<<<<< HEAD
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
=======
        const cj = await api.queue.current(printerId, {
          timeout: 8000,
        });
        if (aborted || !mountedRef.current) return;

<<<<<<< Updated upstream
        // cj: { queue_number, file_name, thumbnail_url, job_id, status, started_at, time_min, remaining_min, material?, template?, employee_id? }
        const tm = Number(cj?.time_min ?? cj?.timeMin ?? 0);
=======
        const tm = Number(
          cj?.time_min ?? cj?.timeMin ?? 0
        );
>>>>>>> Stashed changes
        const stIso = cj?.started_at ?? cj?.startedAt ?? null;
        const remMin =
          cj?.remaining_min ?? cj?.remainingMin ?? null;

        // เก็บ material จาก current job (ลองหลายฟิลด์)
        const mat =
          cj?.material ||
          cj?.template?.material ||
          cj?.manifest?.material ||
          (Array.isArray(cj?.filaments) &&
            cj.filaments[0]?.material) ||
          null;
        if (mat) setMaterial(String(mat).toUpperCase());

        if (tm > 0 && noEstimate) setEstimatedSeconds(tm * 60);
<<<<<<< Updated upstream
        if (stIso && noStart) setStartedAt(typeof stIso === "number" ? new Date(stIso).toISOString() : stIso);
=======
        if (stIso && noStart) {
          const next =
            typeof stIso === "number"
              ? new Date(stIso).toISOString()
              : stIso;
          setStartedAt(next);
        }
>>>>>>> Stashed changes

        // คำนวณ remainingSeconds จากข้อมูล BE โดยตรง
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

<<<<<<< Updated upstream
        // ตั้งชื่อ/รูป และ queue number + owner
        setCurrentJob((prev) => ({
          name: cj?.file_name || prev?.name || "File Name",
          // ✅ ถ้า BE ไม่มี thumbnail_url → ใช้รูป fallback
          thumb: cj?.thumbnail_url || prev?.thumb || NO_IMAGE_URL,
          durationMin: tm || prev?.durationMin,
          startedAt: stIso || prev?.startedAt,
          completion: prev?.completion, // ไม่มี % จาก BE ก็รักษาค่าเดิมไว้
          employee_id: cj?.employee_id ?? prev?.employee_id ?? null,
        }));
        if (cj?.employee_id) setCurrentOwnerEmp(String(cj.employee_id));
        if (cj?.queue_number != null) setCurrentQueueNumber(String(cj.queue_number).padStart(3, "0"));
      } catch {
        // เงียบไว้
=======
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
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
        };

        if (!shallowEq(prev || {}, mergedJob)) {
          setCurrentJob(mergedJob);
          lastJobRef.current = mergedJob;
        }
<<<<<<< HEAD

        // queue number → แสดง "001", "002", ...
        let qn = currentQueueNumber;
        if (res.queue_number != null) {
          const padded = String(res.queue_number).padStart(3, "0");
          if (padded !== qn) {
            qn = padded;
            setCurrentQueueNumber(padded);
          }
=======
        if (cj?.queue_number != null) {
          const qn = String(cj.queue_number).padStart(3, "0");
          if (qn !== currentQueueNumber)
            setCurrentQueueNumber(qn);
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
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
>>>>>>> Stashed changes
      }
    };

<<<<<<< HEAD
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
=======
    runFallback();
<<<<<<< Updated upstream
    return () => { aborted = true; };
  }, [api, printerId, printState, estimatedSeconds, startedAt]);
=======
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
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
>>>>>>> Stashed changes

  // ถ้ายังไม่มี owner จาก fallback ให้ลองถามเป็นครั้งคราว (น้ำหนักเบา)
  useEffect(() => {
    let stop = false;
    async function fillOwnerIfMissing() {
      if (printState !== "printing") return;
      if (currentOwnerEmp) return;
      try {
        const cj = await api.queue.current(printerId, { timeout: 5000 });
        if (stop) return;
        if (cj?.employee_id) {
          setCurrentOwnerEmp(String(cj.employee_id));
          setCurrentJob((prev) => prev ? { ...prev, employee_id: String(cj.employee_id) } : prev);
        }
      } catch {}
    }
    fillOwnerIfMissing();
    return () => { stop = true; };
  }, [api, printerId, printState, currentOwnerEmp]);

  // ---- คำนวณสิทธิ์ควบคุม (เฉพาะเจ้าของงานหรือผู้จัดการ) ----
  const myEmp = String(user?.employee_id ?? user?.id ?? "");
  const isManager = !!(user?.can_manage_queue || user?.is_manager || user?.role === "manager");
  const ownerEmp = String(currentJob?.employee_id ?? currentOwnerEmp ?? "");
  const isMine = !!myEmp && !!ownerEmp && myEmp === ownerEmp;
  const canControl = isManager || isMine;

  // ---- จัดรูป remainingTime เป็นข้อความ แสดงใน RightPanel ----
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
            state={printState}           // printing | paused | ready | error | offline | idle
            job={currentJob}             // { name, thumb, durationMin?, startedAt?, completion?, employee_id? }
            queueNumber={currentQueueNumber}
            canControl={canControl}
          />
        </div>
      </div>

      <div className="right-col">
        {/* ส่ง printerId เพื่อให้ปุ่มใน RightPanel เรียก OctoPrint ได้จริง */}
        {/* ส่ง remainingTime + material ไปแสดงในแผงขวา */}
        <RightPanel
          printerId={printerId}
          remainingTime={remainingText}
          material={material}
          canControl={canControl}
        />
      </div>
    </div>
  );
}