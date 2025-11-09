# backend/auth_routes.py
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from jose import jwt  # ใช้สำหรับอ่าน claims โดยไม่ต้อง verify เพื่อดึง exp

from db import get_db
from models import User
from schemas import (
    LoginIn,
    LoginOut,
    RefreshIn,
    UserOut,
)
from auth import (
    create_access_token,                 # fallback
    create_access_token_for_user,        # ✅ ใส่ claims ผู้ใช้
    create_refresh_token,
    decode_refresh_token,
    get_user_from_header_or_query,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# รับทั้ง 6–7 หลัก และเผื่อ EN นำหน้า
_DIGITS_RE = re.compile(r"^\d{6,7}$")


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    raw = payload.employee_id.strip().upper()
    emp = re.sub(r"^EN", "", raw)
    if not _DIGITS_RE.match(emp):
        raise HTTPException(status_code=422, detail="Invalid Employee ID (6–7 digits)")

    user = db.query(User).filter(User.employee_id == emp).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    # ใส่ claims ครบ (confirmed/can_manage_queue/token_version)
    access_token = create_access_token_for_user(user)
    refresh_token = create_refresh_token(sub=user.employee_id)

    # อัปเดต last_login ถ้ายืนยันโปรไฟล์แล้ว
    needs_confirm = not bool(user.confirmed)
    if not needs_confirm:
        user.last_login_at = datetime.utcnow()
        db.add(user); db.commit(); db.refresh(user)

    return LoginOut(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        needs_confirm=needs_confirm,
    )


@router.post("/refresh")
def refresh(payload: RefreshIn, db: Session = Depends(get_db)):
    """
    รับ refresh_token แล้วออก access_token ใหม่ (ถ้าเจอ user จะใส่ claims ครบ)
    """
    if not payload.refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token required")

    data = decode_refresh_token(payload.refresh_token)
    sub = data.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = db.query(User).filter(User.employee_id == str(sub)).first()
    at = create_access_token_for_user(user) if user else create_access_token(sub=str(sub))
    return {"access_token": at, "token_type": "bearer"}


def _read_token_exp_from_auth_header(request: Request) -> Optional[int]:
    """
    อ่าน exp (unix seconds) จาก access token ใน Authorization header
    โดยไม่ verify ลายเซ็น (แค่ดึง claims มาใช้ client-side UX)
    """
    try:
        authz = request.headers.get("Authorization", "")
        if not authz.startswith("Bearer "):
            return None
        token = authz.split(" ", 1)[1]
        claims = jwt.get_unverified_claims(token)
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


@router.get("/me")
def me(request: Request, user: User = Depends(get_user_from_header_or_query)):
    """
    คืนข้อมูลผู้ใช้ + token_exp + server_time
    - token_exp: เวลาหมดอายุของ access token (unix seconds) ถ้ามี header
    - server_time: เวลาปัจจุบันฝั่งเซิร์ฟเวอร์ (unix seconds)
    NOTE: ไม่ใส่ response_model เพื่อเปิดทางให้ฝั่ง FE รับ field เสริมได้อิสระ
    """
    user_out = UserOut.model_validate(user).model_dump(mode="json")
    token_exp = _read_token_exp_from_auth_header(request)
    server_time = int(datetime.utcnow().timestamp())
    return {
        **user_out,
        "token_exp": token_exp,
        "server_time": server_time,
    }


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout():
    # Stateless JWT: ฝั่ง FE ลบทิ้งจาก storage/cookie ก็พอ
    return {"ok": True}
