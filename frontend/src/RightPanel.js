// src/RightPanel.js
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useLayoutEffect,
} from "react";
import "./RightPanel.css";
import { useApi } from "./api";

const DEFAULTS = { nozzle: 220, bed: 65, feed: 100 };
const MIN_FEED = 10,
  MAX_FEED = 200;

// ---------- Material options ----------
const MATERIAL_TYPES = ["PLA", "ABS", "PETG", "ASA", "TPU"];
const MATERIAL_SWATCHES = [
  { name: "White", hex: "#FFFFFF", text: "#111" },
  { name: "Black", hex: "#111111", text: "#fff" },
  { name: "Gray", hex: "#BDBDBD", text: "#111" },
  { name: "Red", hex: "#E53935", text: "#fff" },
  { name: "Orange", hex: "#FB8C00", text: "#111" },
  { name: "Yellow", hex: "#FDD835", text: "#111" },
  { name: "Green", hex: "#43A047", text: "#fff" },
  { name: "Blue", hex: "#1E88E5", text: "#fff" },
  { name: "Purple", hex: "#8E24AA", text: "#fff" },
  { name: "Pink", hex: "#EC407A", text: "#fff" },
  { name: "Brown", hex: "#6D4C41", text: "#fff" },
  { name: "Beige", hex: "#EED9C4", text: "#111" },
];

// ---------- small helper ----------
function useClickOutside(ref, onClose) {
  useEffect(() => {
    const h = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onClose?.();
    };
    document.addEventListener("mousedown", h);
    document.addEventListener("touchstart", h);
    return () => {
      document.removeEventListener("mousedown", h);
      document.removeEventListener("touchstart", h);
    };
  }, [ref, onClose]);
}

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
}) {
  const api = useApi();

  // Temps
  const [nozzleActual, setNozzleActual] = useState(DEFAULTS.nozzle);
  const [nozzleTarget, setNozzleTarget] = useState(DEFAULTS.nozzle);
  const [bedActual, setBedActual] = useState(DEFAULTS.bed);
  const [bedTarget, setBedTarget] = useState(DEFAULTS.bed);

  // Speed
  const [feedrate, setFeedrate] = useState(DEFAULTS.feed);
  const [speedBusy, setSpeedBusy] = useState(false);

  // Temp modal
  const [activeModal, setActiveModal] = useState(null); // 'nozzle' | 'bed' | null
  const [draftTemp, setDraftTemp] = useState(0);
  const [modalBusy, setModalBusy] = useState(false);

  // Material popover
  const [matType, setMatType] = useState("PLA");
  const [matColor, setMatColor] = useState("#1E88E5");
  const [matText, setMatText] = useState("#fff");
  const [matOpen, setMatOpen] = useState(false);
  const matAnchorRef = useRef(null);
  const popRef = useRef(null);
  const [matPos, setMatPos] = useState({
    top: 0,
    left: 0,
    placement: "bottom",
    arrowX: 40,
  });

  // ================= Material popover position =================
  function computeMaterialPopoverPosition(anchorEl, popEl) {
    const GAP = 8; // space between anchor <-> pop
    const PAD = 8; // viewport padding
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const ar = anchorEl.getBoundingClientRect();
    const pw = Math.max(1, popEl.offsetWidth || 280);
    const ph = Math.max(1, popEl.offsetHeight || 240);

    // start centered with the trigger
    let left = ar.left + ar.width / 2 - pw / 2;
    left = Math.max(PAD, Math.min(vw - PAD - pw, left));

    // bottom or top
    let placement = "bottom";
    let top = ar.bottom + GAP;
    const canPlaceBottom = top + ph <= vh - PAD;
    const canPlaceTop = ar.top - GAP - ph >= PAD;

    if (!canPlaceBottom && canPlaceTop) {
      top = ar.top - GAP - ph;
      placement = "top";
    } else if (!canPlaceBottom && !canPlaceTop) {
      // extremely tight viewport: clamp inside
      top = Math.max(PAD, Math.min(vh - PAD - ph, top));
    }

    // arrow: point to anchor center but clamp inside pop width
    const anchorCenterX = ar.left + ar.width / 2;
    let arrowX = anchorCenterX - left;
    arrowX = Math.max(12, Math.min(pw - 12, arrowX));

    return { top, left, placement, arrowX };
  }

  useClickOutside(popRef, () => setMatOpen(false));

  useLayoutEffect(() => {
    if (!matOpen) return;
    const place = () => {
      const a = matAnchorRef.current,
        p = popRef.current;
      if (!a || !p) return;
      setMatPos(computeMaterialPopoverPosition(a, p));
    };
    // wait 1 frame so DOM has real size
    const raf = requestAnimationFrame(place);

    const re = () => place();
    window.addEventListener("resize", re);
    window.addEventListener("scroll", re, true);
    const onKey = (e) => {
      if (e.key === "Escape") {
        setMatOpen(false);
        matAnchorRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", re);
      window.removeEventListener("scroll", re, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [matOpen]);

  // restore saved values
  useEffect(() => {
    try {
      const raw = localStorage.getItem("printer_setpoints");
      if (raw) {
        const sp = JSON.parse(raw);
        if (Number.isFinite(sp?.nozzle) && sp.nozzle > 0) setNozzleTarget(sp.nozzle);
        if (Number.isFinite(sp?.bed) && sp.bed > 0) setBedTarget(sp.bed);
      }
      const f = localStorage.getItem("printer_feedrate");
      if (f && Number.isFinite(+f)) setFeedrate(+f);

      const t = localStorage.getItem("mat_type");
      const c = localStorage.getItem("mat_color");
      const x = localStorage.getItem("mat_text");
      if (t) setMatType(t);
      if (c) setMatColor(c);
      if (x) setMatText(x);
    } catch {}
  }, []);

  // polling temps
  useEffect(() => {
    if (!printerId) return;
    let stop = false,
      to;
    const num = (v) => (Number.isFinite(+v) ? Math.round(+v) : null);
    const tick = async () => {
      try {
        const r = await api.printer.temps(printerId, { timeoutMs: 12000 });
        const noz = r?.nozzle ?? r?.temperature?.tool0 ?? {};
        const bd = r?.bed ?? r?.temperature?.bed ?? {};
        const nAct = num(noz.actual),
          nTgt = num(noz.target);
        const bAct = num(bd.actual),
          bTgt = num(bd.target);
        if (nAct !== null) setNozzleActual(nAct);
        if (bAct !== null) setBedActual(bAct);
        if (nTgt !== null && nTgt > 0) setNozzleTarget(nTgt);
        if (bTgt !== null && bTgt > 0) setBedTarget(bTgt);
      } catch {
      } finally {
        if (!stop) to = setTimeout(tick, 3000);
      }
    };
    tick();
    return () => {
      stop = true;
      if (to) clearTimeout(to);
    };
  }, [api, printerId]);

  // open/close temp modal
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

  // commit temps
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
          localStorage.setItem(
            "printer_setpoints",
            JSON.stringify({
              nozzle: activeModal === "nozzle" ? finalValue : nozzleTarget,
              bed: activeModal === "bed" ? finalValue : bedTarget,
            })
          );
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

  // speed
  const changeFeed = async (next) => {
    const clamped = Math.max(MIN_FEED, Math.min(MAX_FEED, next));
    if (clamped === feedrate || speedBusy) return;
    setSpeedBusy(true);
    try {
      await api.printer.setFeedrate(printerId, clamped, { timeoutMs: 8000 });
      setFeedrate(clamped);
      try {
        localStorage.setItem("printer_feedrate", String(clamped));
      } catch {}
    } catch (e) {
      alert(e?.message || "Failed to change speed");
    } finally {
      setSpeedBusy(false);
    }
  };
  const stepFeed = (d) => changeFeed(feedrate + d);
  const holdTimerRef = useRef(null);
  const startHold = (d) => {
    stepFeed(d);
    clearInterval(holdTimerRef.current);
    holdTimerRef.current = setInterval(() => stepFeed(d), 120);
  };
  const stopHold = () => {
    clearInterval(holdTimerRef.current);
    holdTimerRef.current = null;
  };
  useEffect(() => () => stopHold(), []);
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

  // material selections (เลือกแล้วปิด)
  const pickType = (t) => {
    setMatType(t);
    try {
      localStorage.setItem("mat_type", t);
    } catch {}
    setMatOpen(false);
    matAnchorRef.current?.focus();
  };
  const pickColor = (hex, text) => {
    setMatColor(hex);
    setMatText(text || "#111");
    try {
      localStorage.setItem("mat_color", hex);
      localStorage.setItem("mat_text", text || "#111");
    } catch {}
    setMatOpen(false);
    matAnchorRef.current?.focus();
  };

  const remainingTime = useMemo(() => externalRemainingTime ?? "-", [externalRemainingTime]);

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

      {/* Remaining + Material */}
      <div className="time-material-row">
        <div className="card time-card">
          <div className="card-header center">
            <img src="/icon/Time.png" alt="" className="icon" />
            <div className="label">Remaining Time</div>
          </div>
          <div className="value">{remainingTime}</div>
        </div>

        <div className="card material-card" style={{ position: "relative" }}>
          <div className="card-header center">
            <img src="/icon/Material.png" alt="" className="icon" />
            <div className="label">Material</div>
          </div>

          {/* Trigger */}
          <button
            ref={matAnchorRef}
            className="mat-circle"
            onClick={() => setMatOpen(v => !v)}
            aria-haspopup="dialog"
            aria-expanded={matOpen}
            style={{
              background: matColor,
              color: matText,
            }}
          >
            {matType}
          </button>

          {/* Popover (fixed positioning from computed state) */}
          {matOpen && (
            <div
              ref={popRef}
              className={`mat-popover ${
                matPos.placement === "top" ? "is-top" : "is-bottom"
              }`}
              role="dialog"
              aria-label="Material picker"
              style={{
                position: "fixed",
                top: `${matPos.top}px`,
                left: `${matPos.left}px`,
                "--arrow-x": `${Math.round(matPos.arrowX)}px`,
              }}
            >
              <div className="mat-type-row" role="radiogroup" aria-label="Material type">
                {MATERIAL_TYPES.map((t) => (
                  <button
                    key={t}
                    className={`mat-type ${t === matType ? "is-active" : ""}`}
                    aria-pressed={t === matType}
                    onClick={() => {
                      setMatType(t);
                      try {
                        localStorage.setItem("mat_type", t);
                      } catch {}
                      setMatOpen(false);
                      matAnchorRef.current?.focus();
                    }}
                  >
                    {t}
                  </button>
                ))}
              </div>

              <div className="mat-divider" />

              <div className="mat-grid" role="listbox" aria-label="Material color">
                {MATERIAL_SWATCHES.map((s) => {
                  const selected =
                    (matColor || "").toLowerCase() === s.hex.toLowerCase();
                  return (
                    <button
                      key={s.hex}
                      title={s.name}
                      className={`mat-swatch ${selected ? "is-selected" : ""}`}
                      aria-selected={selected}
                      style={{ background: s.hex }}
                      onClick={() => {
                        setMatColor(s.hex);
                        setMatText(s.text || "#111");
                        try {
                          localStorage.setItem("mat_color", s.hex);
                          localStorage.setItem("mat_text", s.text || "#111");
                        } catch {}
                        setMatOpen(false);
                        matAnchorRef.current?.focus();
                      }}
                    />
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Temp modal */}
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
