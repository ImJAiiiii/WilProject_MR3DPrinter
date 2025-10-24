// src/Login.js
import React, { useEffect, useMemo, useState } from "react";
import { useAuth } from "./auth/AuthContext";
import "./Login.css";

// เลขพนักงาน: เลขล้วน 6–7 หลัก
const ID_REGEX = /^\d{6,7}$/;

// แสดง EN นำหน้าเฉพาะตอนโชว์ (ไม่ได้แก้ค่าที่เก็บ)
const fmtId = (id) => {
  const s = (id ?? "").toString().trim();
  if (!s) return "";
  return s.toUpperCase().startsWith("EN") ? s.toUpperCase() : `EN${s}`;
};

export default function Login() {
  const {
    loginWithEmployeeId,
    confirmFirstLogin,
    cancelPendingLogin,
    pendingUser, // <-- ดึงจาก Context แทนการเก็บเอง
  } = useAuth();

  // -------- Step: enter --------
  const [employeeId, setEmployeeId] = useState(""); // เก็บเป็น "เลขล้วน"
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const digitsLen = (employeeId || "").trim().length;
  const isValid = useMemo(() => ID_REGEX.test(employeeId.trim()), [employeeId]);

  // รองรับพิมพ์ ENxxxx ด้วย แต่เก็บเฉพาะเลขล้วนสูงสุด 7 หลัก
  const onChangeId = (e) => {
    const raw = e.target.value.toUpperCase();
    const digitsOnly = raw.replace(/^EN/i, "").replace(/\D/g, "").slice(0, 7);
    setEmployeeId(digitsOnly);
  };

  // -------- Step machine --------
  // ถ้ามี pendingUser จาก Context ให้แสดงหน้าคอนเฟิร์มอัตโนมัติ
  const step = pendingUser ? "confirm" : "enter";

  // ===== Confirm page (state เฉพาะหน้าคอนเฟิร์ม) =====
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState("");

  // ได้ pendingUser ใหม่ → sync ชื่อเข้า input
  useEffect(() => {
    if (pendingUser) setEditName(pendingUser.name || "");
  }, [pendingUser]);

  const submitId = async (e) => {
    e.preventDefault();
    setErr("");
    if (!isValid) {
      setErr("กรุณากรอกหมายเลขพนักงาน 6–7 หลัก");
      return;
    }
    setLoading(true);
    try {
      const res = await loginWithEmployeeId(employeeId);
      // res.step: "confirm" | "ok"
      // - ถ้า "confirm" → Context จะใส่ pendingUser ให้แล้ว หน้านี้จะเปลี่ยนเป็น confirm เอง
      // - ถ้า "ok" → App หลักจะเปลี่ยนเป็นหน้าหลังล็อกอินเอง
      if (res?.step === "ok") {
        // nothing; App จะเปลี่ยน route/render ให้เอง
      }
    } catch (ex) {
      setErr(ex?.message || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  const confirm = async () => {
    if (!pendingUser) return;
    setErr("");
    setLoading(true);
    try {
      const finalName = (editName || pendingUser.name || "").trim();
      if (!finalName) {
        setErr("กรุณากรอกชื่อสำหรับแสดงผล");
        setLoading(false);
        return;
      }
      await confirmFirstLogin(pendingUser.id, finalName, pendingUser.email);
      // สำเร็จแล้ว App จะเห็น user และสลับเข้าแอปให้อัตโนมัติ
    } catch (ex) {
      setErr(ex?.message || "Confirm failed");
    } finally {
      setLoading(false);
    }
  };

  const backToEnter = () => {
    // กลับไปหน้ากรอกรหัส + เคลียร์ pending token/user ใน Context
    cancelPendingLogin?.();
    setIsEditing(false);
    setErr("");
  };

  const BRAND_LOGO = process.env.PUBLIC_URL + "/images/logologin.png";
  const initial = ((editName || pendingUser?.name || "E").trim().charAt(0) || "E").toUpperCase();

  return (
    <div className="login-screen">
      {/* HERO */}
      <div className="login-hero">
        <img className="hero-logo" src={BRAND_LOGO} alt="Analog Devices" />
        <h1 className="hero-title">Analog Devices</h1>
        <div className="hero-sub">3D Printer Console</div>
      </div>

      {/* PANEL */}
      <div className="login-panel">
        {step === "enter" && (
          <form className="login-form" onSubmit={submitId} noValidate>
            <label className="login-label">Employee Number</label>
            <input
              value={employeeId}
              onChange={onChangeId}
              placeholder="กรอกหมายเลขพนักงาน 6–7 หลัก"
              maxLength={9}                 // เผื่อผู้ใช้พิมพ์ EN มา เราดึงเลขออกเอง
              inputMode="numeric"           // มือถือ: แสดงคีย์บอร์ดตัวเลข
              pattern="[0-9]{6,7}"
              title="กรอกหมายเลขพนักงาน 6–7 หลัก (ไม่ต้องใส่ EN)"
              autoFocus
              autoComplete="off"
            />
            <div className={`login-hint ${isValid ? "ok" : (digitsLen ? "info" : "warn")}`}>
              {digitsLen === 0
                ? "กรอกหมายเลขพนักงาน 6–7 หลัก (ไม่ต้องใส่ EN)"
                : (isValid
                    ? `✓ รูปแบบถูกต้อง (${digitsLen} หลัก)`
                    : `ต้องเพิ่มอีก ${Math.max(0, 6 - digitsLen)} หลัก`)}
            </div>

            {err && <div className="login-error">{err}</div>}

            <button className="btn-primary" type="submit" disabled={loading || !isValid}>
              {loading ? "Checking..." : "Continue"}
            </button>

            <div className="login-note">
              ครั้งแรก: ระบบจะแสดงชื่อให้ยืนยัน • ครั้งต่อไป: ใส่หมายเลขพนักงานแล้วเข้าได้เลย
            </div>
          </form>
        )}

        {step === "confirm" && pendingUser && (
          <div className="confirm-card">
            <div className="confirm-head">
              <div className="confirm-avatar">{initial}</div>

              <div className="confirm-meta">
                {/* บรรทัดชื่อ + ปุ่มแก้ */}
                <div className="confirm-name">
                  {!isEditing ? (
                    <>
                      <span className="name-text">{editName || pendingUser.name || "—"}</span>
                      <button
                        type="button"
                        className="confirm-edit"
                        onClick={() => setIsEditing(true)}
                      >
                        Edit
                      </button>
                    </>
                  ) : (
                    <div className="name-edit-row">
                      <input
                        className="name-input"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        placeholder="Your display name"
                        autoFocus
                      />
                      <button
                        type="button"
                        className="confirm-edit save"
                        onClick={() => setIsEditing(false)}
                      >
                        Save
                      </button>
                    </div>
                  )}
                </div>

                {/* แสดง EN นำหน้าเฉพาะตอนโชว์ */}
                <div className="confirm-id">
                  Employee Number: <b>{fmtId(pendingUser.employee_id || pendingUser.id)}</b>
                </div>

                {!!pendingUser.email && <div className="confirm-email">{pendingUser.email}</div>}
              </div>
            </div>

            {err && <div className="login-error" style={{ marginTop: 10 }}>{err}</div>}

            <div className="confirm-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={confirm}
                disabled={loading || !(editName || pendingUser.name)}
              >
                {loading ? "Signing in..." : "Confirm & Sign in"}
              </button>

              <button
                type="button"
                className="btn btn--ghost"
                onClick={backToEnter}
                disabled={loading}
              >
                Back
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
