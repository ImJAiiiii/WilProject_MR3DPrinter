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
 * - Fallback: ดึงเวลาจาก BE /api/printers/:id/current-job ถ้า OctoPrint ไม่ให้เวลา
 */
export default function MonitorPage({
  printerId = process.env.REACT_APP_PRINTER_ID || "prusa-core-one",
}) {
  const api = useApi();
  const { user } = useAuth() || {};

  // ---- state หลัก ----
  // เริ่มต้นให้รีล: ยังไม่เชื่อม → ออฟไลน์/รอเชื่อมต่อ
  const [printerOnline, setPrinterOnline] = useState(false);
  const [printerStatus, setPrinterStatus] = useState("Offline — waiting for connection"); // status_text
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
    return () => { mountedRef.current = false; };
  }, []);

  // ---- SSE: /printers/:id/status/stream → online/state/status_text/progress ----
  const sseCloserRef = useRef(null);
  const lastSseAtRef = useRef(0);

  useEffect(() => {
    // ปิด connection เก่าก่อน
    try { sseCloserRef.current?.close?.(); } catch {}
    sseCloserRef.current = null;

    const path = `/printers/${encodeURIComponent(printerId)}/status/stream`;
    const closer = api.sseWithBackoff(path, {
      withToken: true,
      onOpen: () => { lastSseAtRef.current = Date.now(); },
      onMessage: (ev) => {
        if (!ev || typeof ev.data !== "string" || !ev.data.trim()) return;
        try {
          const msg = JSON.parse(ev.data);
          // backend ส่ง { type: "status", data: PrinterStatusOut }
          const s = msg?.data || msg;
          const online = typeof s?.is_online === "boolean" ? s.is_online : !!s?.online;
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
            setCurrentJob((prev) => prev && prev.completion === 100 ? prev : null);
            setMaterial(null);
          }
        } catch {
          // ignore malformed payload
        }
      },
      onError: () => {
        if (!mountedRef.current) return;
        setPrinterOnline(false);
        setPrintState("offline");
        setPrinterStatus("Offline — waiting for connection");
      },
    });
    sseCloserRef.current = closer;

    return () => {
      try { closer?.close?.(); } catch {}
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
      }
    }, 5000);
    return () => clearInterval(t);
  }, []);

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

      try {
        // ใช้ octoprintJobSafe ซึ่งจะทำ backoff + ตั้ง cooldown ให้เองเมื่อเจอ 502
        const data = await api.printer.octoprintJobSafe(printerId, { timeoutMs: 12000 });
        if (!mountedRef.current) return;
        if (!data) { // อยู่ช่วง cooldown → ยังไม่อัปเดตอะไร ปล่อยให้รอบหน้า
          scheduleNext();
          return;
        }

        // payload: { octoprint: { job, progress }, ... }
        const job = data?.octoprint?.job || {};
        const progress = data?.octoprint?.progress || {};
        const stateText = String(data?.octoprint?.state || "");
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
          printTime > 0 && printTimeLeft > 0 ? printTime + printTimeLeft : 0;
        const estimatedTotal =
          estimatedFromProgress || Number(job?.estimatedPrintTime ?? 0) || 0;

        setEstimatedSeconds(estimatedTotal || 0);
        setRemainingSeconds(
          printTimeLeft > 0
            ? printTimeLeft
            : estimatedTotal > 0
            ? Math.max(0, estimatedTotal - printTime)
            : null
        );
        setStartedAt(
          printTime > 0
            ? new Date(Date.now() - printTime * 1000).toISOString()
            : null
        );

        setCurrentJob({
          name: fileName || "File Name",
          thumb: NO_IMAGE_URL, // ใช้รูปสำรองเป็นค่าเริ่มต้น
          durationMin: estimatedTotal ? Math.round(estimatedTotal / 60) : undefined,
          startedAt: printTime > 0 ? new Date(Date.now() - printTime * 1000).toISOString() : undefined,
          completion,
          employee_id: currentOwnerEmp || null, // ยังไม่รู้จาก Octo → รอ fallback เติม
        });

        // ถ้ามี queue number ที่ backend อื่นส่งมา สามารถเซ็ตได้ที่นี่:
        // setCurrentQueueNumber(...)
      } catch (e) {
        // เงียบไว้ (safe variant จะตั้ง cooldown เองหากเป็น 502)
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
  }, [api, printerId, printState, currentOwnerEmp]);

  // ---- Fallback: ถ้า OctoPrint ไม่ส่งเวลา ให้ใช้ข้อมูลจาก BE current-job ----
  useEffect(() => {
    let aborted = false;

    async function runFallback() {
      // เงื่อนไขใช้ fallback เฉพาะตอนกำลังพิมพ์ และไม่มีเวลาที่เชื่อถือได้
      const noEstimate = !(estimatedSeconds > 0);
      const noStart = !startedAt;
      if (printState !== "printing" || (!noEstimate && !noStart)) return;

      try {
        const cj = await api.queue.current(printerId, { timeout: 8000 });
        if (aborted || !mountedRef.current) return;

        // cj: { queue_number, file_name, thumbnail_url, job_id, status, started_at, time_min, remaining_min, material?, template?, employee_id? }
        const tm = Number(cj?.time_min ?? cj?.timeMin ?? 0);
        const stIso = cj?.started_at ?? cj?.startedAt ?? null;
        const remMin = cj?.remaining_min ?? cj?.remainingMin ?? null;

        // เก็บ material จาก current job (ลองหลายฟิลด์)
        const mat =
          cj?.material ||
          cj?.template?.material ||
          cj?.manifest?.material ||
          (Array.isArray(cj?.filaments) && cj.filaments[0]?.material) ||
          null;
        if (mat) setMaterial(String(mat).toUpperCase());

        if (tm > 0 && noEstimate) setEstimatedSeconds(tm * 60);
        if (stIso && noStart) setStartedAt(typeof stIso === "number" ? new Date(stIso).toISOString() : stIso);

        // คำนวณ remainingSeconds จากข้อมูล BE โดยตรง
        if (remMin != null) {
          setRemainingSeconds(Math.max(0, Math.round(remMin * 60)));
        } else if (tm > 0 && stIso) {
          const elapsed = Math.max(0, Math.floor((Date.now() - new Date(stIso).getTime()) / 1000));
          setRemainingSeconds(Math.max(0, tm * 60 - elapsed));
        }

        // ตั้งชื่อ/รูป และ queue number + owner
        setCurrentJob((prev) => ({
          name: cj?.file_name || prev?.name || "File Name",
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
      }
    }

    runFallback();
    return () => { aborted = true; };
  }, [api, printerId, printState, estimatedSeconds, startedAt]);

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
          <PrinterStatus status={printerStatus} printerOnline={printerOnline} />

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
