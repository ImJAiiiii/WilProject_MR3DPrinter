// src/RightPanel.js
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./RightPanel.css";
import { useApi } from "./api";

const DEFAULTS = { nozzle: 220, bed: 65, feed: 100 }; // ค่าเริ่มต้นทั่วไป (PLA)
const MIN_FEED = 10;
const MAX_FEED = 200;

// สีคร่าว ๆ ตามชนิดวัสดุ
const MATERIAL_COLORS = {
  PLA: "#4caf50",
  PETG: "#2196f3",
  ABS: "#ff9800",
  ASA: "#9c27b0",
  TPU: "#009688",
  DEFAULT: "#9e9e9e",
};

/* ================= Temperature Modal ================ */
function TemperatureModal({
  title,
  draft,
  setDraft,
  onCancel,
  onDone,
  min = 0,
  max = 999,
  busy = false,
}) {
  const [text, setText] = useState(String(draft));
  const closeBtnRef = useRef(null);

  useEffect(() => setText(String(draft)), [draft]);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeBtnRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && !busy && onCancel();
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [onCancel, busy]);

  const clamp = (n) => Math.min(max, Math.max(min, n));
  const nudge = (d) => {
    const base = parseInt(text || draft, 10) || 0;
    const next = clamp(base + d);
    setText(String(next));
    setDraft(next);
  };

  const onChange = (e) => {
    const onlyDigits = e.target.value.replace(/[^\d]/g, "");
    setText(onlyDigits);
    if (onlyDigits !== "") setDraft(clamp(parseInt(onlyDigits, 10)));
  };

  const commit = () => {
    if (busy) return;
    const n = clamp(parseInt(text || draft, 10) || draft);
    setText(String(n));
    setDraft(n);
    onDone(n);
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      nudge(+1);
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      nudge(-1);
    }
  };

  return (
    <div
      className="temp-modal-overlay"
      role="dialog"
      aria-modal="true"
      onClick={() => !busy && onCancel()}
    >
      <div className="temp-modal" onClick={(e) => e.stopPropagation()}>
        <button
          ref={closeBtnRef}
          className="temp-close"
          aria-label="Close"
          onClick={onCancel}
          disabled={busy}
        >
          <img src={process.env.PUBLIC_URL + "/icon/cancelprint.png"} alt="" />
        </button>

        <h2 className="temp-title">{title}</h2>

        <input
          className="temp-input"
          value={text}
          onChange={onChange}
          onKeyDown={onKeyDown}
          inputMode="numeric"
          pattern="\d*"
          aria-label="Temperature setpoint"
          disabled={busy}
        />

        <div className="temp-controls">
          <button type="button" className="temp-btn" onClick={() => nudge(-5)} disabled={busy}>
            −5
          </button>
          <button type="button" className="temp-btn" onClick={() => nudge(+5)} disabled={busy}>
            +5
          </button>
          <button type="button" className="temp-btn" onClick={() => nudge(-30)} disabled={busy}>
            −30
          </button>
          <button type="button" className="temp-btn" onClick={() => nudge(+30)} disabled={busy}>
            +30
          </button>
        </div>

        <button type="button" className="done-btn" onClick={commit} disabled={busy}>
          {busy ? "Applying..." : "Done"}
        </button>
      </div>
    </div>
  );
}

/* ================= Right Panel ================ */
export default function RightPanel({
  printerId,
  remainingTime: externalRemainingTime,
  material: externalMaterial,
}) {
  const api = useApi();

  // อุณหภูมิ
  const [nozzleActual, setNozzleActual] = useState(DEFAULTS.nozzle);
  const [nozzleTarget, setNozzleTarget] = useState(DEFAULTS.nozzle);
  const [bedActual, setBedActual] = useState(DEFAULTS.bed);
  const [bedTarget, setBedTarget] = useState(DEFAULTS.bed);

  // ความเร็ว
  const [feedrate, setFeedrate] = useState(DEFAULTS.feed);
  const [speedBusy, setSpeedBusy] = useState(false);

  // Modal
  const [activeModal, setActiveModal] = useState(null); // 'nozzle' | 'bed' | null
  const [draftTemp, setDraftTemp] = useState(0);
  const [modalBusy, setModalBusy] = useState(false);

  const openNozzle = () => {
    setActiveModal("nozzle");
    setDraftTemp(nozzleTarget || DEFAULTS.nozzle);
  };
  const openBed = () => {
    setActiveModal("bed");
    setDraftTemp(bedTarget || DEFAULTS.bed);
  };
  const closeModal = () => !modalBusy && setActiveModal(null);

  const kbOpen = (e, fn) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), fn());

  // Remaining Time
  const remainingTime = useMemo(() => externalRemainingTime ?? "-", [externalRemainingTime]);

  // วัสดุที่จะแสดง
  const materialLabel = useMemo(() => {
    const t = (externalMaterial || "").toString().trim();
    if (!t) return "-";
    const m = t.match(/\b(PLA|PETG|ABS|ASA|TPU)\b/i);
    return (m ? m[1] : t).toUpperCase();
  }, [externalMaterial]);

  const materialColor =
    MATERIAL_COLORS[materialLabel] ??
    (/(^PLA\b)/i.test(materialLabel)
      ? MATERIAL_COLORS.PLA
      : /(PETG)/i.test(materialLabel)
      ? MATERIAL_COLORS.PETG
      : /(ABS)/i.test(materialLabel)
      ? MATERIAL_COLORS.ABS
      : /(ASA)/i.test(materialLabel)
      ? MATERIAL_COLORS.ASA
      : /(TPU)/i.test(materialLabel)
      ? MATERIAL_COLORS.TPU
      : MATERIAL_COLORS.DEFAULT);

  /* ------- โหลด target และ feedrate ล่าสุดจาก localStorage (ถ้ามี) ------- */
  useEffect(() => {
    try {
      if (typeof localStorage === "undefined") return;
      const raw = localStorage.getItem("printer_setpoints");
      if (raw) {
        const sp = JSON.parse(raw);
        if (Number.isFinite(sp?.nozzle) && sp.nozzle > 0) setNozzleTarget(sp.nozzle);
        if (Number.isFinite(sp?.bed) && sp.bed > 0) setBedTarget(sp.bed);
      }
      const f = localStorage.getItem("printer_feedrate");
      if (f && Number.isFinite(+f)) setFeedrate(+f);
    } catch {
      /* ignore */
    }
  }, []);

  /* ------- Poll ค่าจริงจากเครื่อง ------- */
  useEffect(() => {
    if (!printerId) return;
    let stop = false;
    let t;

    const num = (v) => (Number.isFinite(+v) ? Math.round(+v) : null);

    const tick = async () => {
      try {
        const r = await api.printer.temps(printerId, { timeoutMs: 12000 });

        // รองรับทั้ง payload แบบใหม่/เก่า
        const noz = r?.nozzle ?? r?.temperature?.tool0 ?? {};
        const bd = r?.bed ?? r?.temperature?.bed ?? {};

        const nAct = num(noz.actual);
        const nTgt = num(noz.target);
        const bAct = num(bd.actual);
        const bTgt = num(bd.target);

        if (nAct !== null) setNozzleActual(nAct);
        if (bAct !== null) setBedActual(bAct);

        // กัน idle ที่ target ส่ง 0
        if (nTgt !== null && nTgt > 0) setNozzleTarget(nTgt);
        if (bTgt !== null && bTgt > 0) setBedTarget(bTgt);
      } catch {
        /* เงียบไว้ตอน polling */
      } finally {
        if (!stop) t = setTimeout(tick, 3000);
      }
    };

    tick();
    return () => {
      stop = true;
      if (t) clearTimeout(t);
    };
  }, [api, printerId]);

  /* ------- Commit ค่าอุณหภูมิจาก modal ------- */
  const commitDraft = useCallback(
    async (finalValue) => {
      if (!printerId) return;
      setModalBusy(true);
      try {
        if (activeModal === "nozzle") {
          setNozzleTarget(finalValue);
          await api.printer.setToolTemp(printerId, finalValue, { timeoutMs: 8000 });
        } else if (activeModal === "bed") {
          setBedTarget(finalValue);
          await api.printer.setBedTemp(printerId, finalValue, { timeoutMs: 8000 });
        }
        try {
          if (typeof localStorage !== "undefined") {
            localStorage.setItem(
              "printer_setpoints",
              JSON.stringify({
                nozzle: activeModal === "nozzle" ? finalValue : nozzleTarget,
                bed: activeModal === "bed" ? finalValue : bedTarget,
              })
            );
          }
        } catch {}
        setModalBusy(false);
        setActiveModal(null);
      } catch (e) {
        setModalBusy(false);
        alert(e?.message || "Failed to set temperature");
      }
    },
    [activeModal, api, printerId, nozzleTarget, bedTarget]
  );

  /* ------- Speed control ------- */
  const changeFeed = async (next) => {
    const clamped = Math.max(MIN_FEED, Math.min(MAX_FEED, next));
    if (clamped === feedrate || speedBusy) return;
    setSpeedBusy(true);
    try {
      await api.printer.setFeedrate(printerId, clamped, { timeoutMs: 8000 });
      setFeedrate(clamped);
      try {
        if (typeof localStorage !== "undefined") {
          localStorage.setItem("printer_feedrate", String(clamped));
        }
      } catch {}
    } catch (e) {
      alert(e?.message || "Failed to change speed");
    } finally {
      setSpeedBusy(false);
    }
  };

  const stepFeed = (delta) => changeFeed(feedrate + delta);

  // รองรับกดค้าง
  const holdTimerRef = useRef(null);
  const startHold = (delta) => {
    stepFeed(delta);
    if (holdTimerRef.current) clearInterval(holdTimerRef.current);
    holdTimerRef.current = setInterval(() => stepFeed(delta), 120);
  };
  const stopHold = () => {
    if (holdTimerRef.current) {
      clearInterval(holdTimerRef.current);
      holdTimerRef.current = null;
    }
  };

  // cleanup ป้องกันค้างเมื่อออกจากหน้า
  useEffect(() => {
    return () => {
      if (holdTimerRef.current) {
        clearInterval(holdTimerRef.current);
        holdTimerRef.current = null;
      }
    };
  }, []);

  // ปุ่มลูกศรซ้าย/ขวา
  const onSpeedKey = (e) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepFeed(-1);
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      stepFeed(+1);
    }
  };

  /* ------------------ UI ------------------ */
  return (
    <div className="right-panel">
      {/* Nozzle + Bed */}
      <div className="temp-row">
        <div
          className="card nozzle-card hoverable clickable"
          role="button"
          tabIndex={0}
          onClick={openNozzle}
          onKeyDown={(e) => kbOpen(e, openNozzle)}
          aria-label="Adjust nozzle temperature"
        >
          <div className="card-header left">
            <img src="/icon/arrow.png" alt="" className="icon" />
            <div className="label">Nozzle</div>
          </div>
          <div className="value">{nozzleActual}°C</div>
          <div className="target">{nozzleTarget}°C</div>
        </div>

        <div
          className="card bed-card hoverable clickable"
          role="button"
          tabIndex={0}
          onClick={openBed}
          onKeyDown={(e) => kbOpen(e, openBed)}
          aria-label="Adjust bed temperature"
        >
          <div className="card-header left">
            <img src="/icon/arrow.png" alt="" className="icon" />
            <div className="label">Bed</div>
          </div>
          <div className="value">{bedActual}°C</div>
          <div className="target">{bedTarget}°C</div>
        </div>
      </div>

      {/* Speed */}
      <div
        className={`card speed-card hoverable ${speedBusy ? "is-busy" : ""}`}
        role="group"
        aria-label="Speed"
        tabIndex={0}
        onKeyDown={onSpeedKey}
      >
        <button
          type="button"
          className="speed-arrow left"
          aria-label="Decrease speed"
          onMouseDown={() => startHold(-1)}
          onTouchStart={() => startHold(-1)}
          onMouseUp={stopHold}
          onMouseLeave={stopHold}
          onTouchEnd={stopHold}
          onClick={() => stepFeed(-1)}
          disabled={speedBusy || feedrate <= MIN_FEED}
        >
          ‹
        </button>

        <div className="card-header center">
          <img src="/icon/Speed.png" alt="" className="icon" />
          <div className="label">Speed</div>
        </div>
        <div className="value">{feedrate}%</div>
        <div className="target">{feedrate === 100 ? "Normal" : feedrate > 100 ? "Faster" : "Slower"}</div>

        <button
          type="button"
          className="speed-arrow right"
          aria-label="Increase speed"
          onMouseDown={() => startHold(+1)}
          onTouchStart={() => startHold(+1)}
          onMouseUp={stopHold}
          onMouseLeave={stopHold}
          onTouchEnd={stopHold}
          onClick={() => stepFeed(+1)}
          disabled={speedBusy || feedrate >= MAX_FEED}
        >
          ›
        </button>
      </div>

      {/* Remaining Time + Material */}
      <div className="time-material-row">
        <div className="card time-card">
          <div className="card-header center">
            <img src="/icon/Time.png" alt="" className="icon" />
            <div className="label">Remaining Time</div>
          </div>
          <div className="value">{remainingTime}</div>
        </div>

        <div className="card material-card">
          <div className="card-header center">
            <img src="/icon/Material.png" alt="" className="icon" />
            <div className="label">Material</div>
          </div>
          <div className="material-circle" title={materialLabel} style={{ backgroundColor: materialColor }}>
            {materialLabel}
          </div>
        </div>
      </div>

      {/* Modal */}
      {activeModal && (
        <TemperatureModal
          title={activeModal === "nozzle" ? "Nozzle Temperature" : "Bed Temperature"}
          draft={draftTemp}
          setDraft={setDraftTemp}
          onCancel={closeModal}
          onDone={commitDraft}
          min={activeModal === "nozzle" ? 120 : 0}
          max={activeModal === "nozzle" ? 300 : 120}
          busy={modalBusy}
        />
      )}
    </div>
  );
}
