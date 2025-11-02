# backend/auth_routes.py
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

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


@router.get("/me", response_model=UserOut)
def me(user=Depends(get_user_from_header_or_query)):
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout():
    # Stateless JWT: ฝั่ง FE ลบทิ้งจาก storage/cookie ก็พอ
    return {"ok": True}
