// src/Navbar.js
import React, { useEffect, useRef, useState } from 'react';
import './Navbar.css';
import NotificationBell from './NotificationBell';
import { API_BASE } from './api/auth'; // ✅ ใช้ base URL ของ backend ตอน dev

const DEFAULT_AVATAR = '/icon/profile-circle.png'; // fallback

// ✅ แสดง EN นำหน้าเฉพาะตอนโชว์ (ข้อมูลยังเก็บเป็นเลขล้วนเหมือนเดิม)
const formatEmployeeId = (id) => {
  const s = (id ?? '').toString().trim();
  if (!s) return '';
  return s.toUpperCase().startsWith('EN') ? s.toUpperCase() : `EN${s}`;
};

export default function Navbar({ onUploadClick, user, onLogout, onOpenPrinting }) {
  const [uploadIcon, setUploadIcon] = useState('/icon/upload-blue.png');

  // ===== User menu state =====
  const [openUserMenu, setOpenUserMenu] = useState(false);
  const userBtnRef = useRef(null);
  const menuRef = useRef(null);

  // ปิดเมนูเมื่อคลิกนอก/กด ESC
  useEffect(() => {
    if (!openUserMenu) return;
    const onDoc = (e) => {
      if (e.key === 'Escape') setOpenUserMenu(false);
      if (!menuRef.current || !userBtnRef.current) return;
      if (!menuRef.current.contains(e.target) && !userBtnRef.current.contains(e.target)) {
        setOpenUserMenu(false);
      }
    };
    window.addEventListener('mousedown', onDoc);
    window.addEventListener('keydown', onDoc);
    return () => {
      window.removeEventListener('mousedown', onDoc);
      window.removeEventListener('keydown', onDoc);
    };
  }, [openUserMenu]);

  // ข้อมูลผู้ใช้ (รองรับฟิลด์จาก backend: employee_id, avatar_url)
  const displayName =
    user?.name || user?.displayName || user?.fullName || 'Employee';
  const employeeIdRaw =
    user?.employee_id || user?.id || user?.employeeId || '';
  const employeeId = formatEmployeeId(employeeIdRaw);
  const email = user?.email || '';
  const avatarSrc =
    user?.avatar_url ||
    user?.avatarUrl ||
    user?.photoUrl ||
    user?.profileImage ||
    user?.picture ||
    null;

  const handleImgError = (e) => { e.currentTarget.src = process.env.PUBLIC_URL + DEFAULT_AVATAR; };

  return (
    <nav className="navbar">
      {/* ซ้าย: โลโก้ + ชื่อเครื่องพิมพ์ */}
      <div className="navbar-left">
        <img
          src={process.env.PUBLIC_URL + '/images/anloglogo.png'}
          alt="Logo"
          className="navbar-logo"
          draggable="false"
        />
        <span className="navbar-title">3D Printer : Prusa Core One</span>
      </div>

      {/* ขวา: Upload + แจ้งเตือน + โปรไฟล์ */}
      <div className="navbar-right">
        <button
          className="upload-btn"
          onMouseEnter={() => setUploadIcon('/icon/upload-white.png')}
          onMouseLeave={() => setUploadIcon('/icon/upload-blue.png')}
          onClick={onUploadClick}
          aria-label="Upload"
          title="Upload"
        >
          <img
            src={process.env.PUBLIC_URL + uploadIcon}
            alt=""
            className="upload-icon"
            draggable="false"
          />
          Upload
        </button>

        {/* กระดิ่งแจ้งเตือน (SSE + fallback polling) */}
        <NotificationBell
          size={44}
          iconScale={0.8}
          // ✅ ใช้ URL ที่ถูกต้อง + ชี้ไปพอร์ต 8000 ตอน dev
          eventsUrl={`${API_BASE}/notifications/stream`}
          pollUrl={`${API_BASE}/notifications?limit=20`}
          onOpenPrinting={onOpenPrinting}
        />

        {/* ปุ่มผู้ใช้ + เมนู */}
        <div className="user-wrap">
          <button
            ref={userBtnRef}
            className="user-btn"
            aria-haspopup="menu"
            aria-expanded={openUserMenu}
            title={displayName}
            onClick={() => setOpenUserMenu((v) => !v)}
          >
            <img
              src={process.env.PUBLIC_URL + (avatarSrc || DEFAULT_AVATAR)}
              onError={handleImgError}
              alt="User"
              className="user-icon"
              draggable="false"
            />
          </button>

          {openUserMenu && (
            <div className="user-menu" ref={menuRef} role="menu">
              <div className="user-row">
                <div className="user-avatar">
                  <img
                    src={process.env.PUBLIC_URL + (avatarSrc || DEFAULT_AVATAR)}
                    onError={handleImgError}
                    alt=""
                    draggable="false"
                  />
                </div>
                <div className="user-meta">
                  <div className="user-name">{displayName}</div>
                  {/* ✅ โชว์ EN นำหน้า */}
                  {employeeId && <div className="user-id">{employeeId}</div>}
                  {email && <div className="user-email">{email}</div>}
                </div>
              </div>

              <div className="user-sep" />

              <button
                className="user-item danger"
                role="menuitem"
                onClick={() => { setOpenUserMenu(false); onLogout?.(); }}
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </div>
    </nav>
  );
}
