# backend/auth.py
import os
import time
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, status, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError
from sqlalchemy.orm import Session

from db import get_db
from models import User

# ===================== JWT Config =====================
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")  # เปลี่ยนในโปรดักชัน!
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_SECONDS = int(os.getenv("ACCESS_TOKEN_EXPIRE_SECONDS", str(60 * 60 * 8)))
JWT_ISSUER = os.getenv("JWT_ISSUER")       # ใส่แล้วจะตรวจตอน decode
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE")   # ใส่แล้วจะตรวจตอน decode
# python-jose ไม่รองรับ leeway โดยตรง; ค่าด้านล่างจึงไม่ถูกใช้ใน decode()
JWT_CLOCK_SKEW_SECONDS = int(os.getenv("JWT_CLOCK_SKEW_SECONDS", "10"))

bearer = HTTPBearer(auto_error=False)

# ===================== Helpers =====================
def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="api"'},
    )

def extract_token(
    cred: Optional[HTTPAuthorizationCredentials],
    token_q: Optional[str],
) -> Optional[str]:
    """
    ดึง token จาก Header (Bearer) ถ้ามี ถ้าไม่มีก็ใช้ token จาก query string
    """
    if cred and cred.scheme.lower() == "bearer" and cred.credentials:
        return cred.credentials.strip()
    if token_q:
        return token_q.strip()
    return None

def find_user(db: Session, employee_id: str) -> Optional[User]:
    return db.query(User).filter(User.employee_id == employee_id).first()

# ===================== Token API =====================
def create_access_token(
    sub: str,
    extra: Optional[Dict[str, Any]] = None,
    expires_seconds: Optional[int] = None,
) -> str:
    """
    ออกโทเค็น Bearer
    - sub: employee_id (string)
    - extra: claims เพิ่มเติม (เช่น {"confirmed": True})
    - expires_seconds: อายุเฉพาะครั้ง (ถ้าไม่ส่ง ใช้ค่าจาก .env)
    """
    now = int(time.time())
    exp = now + int(expires_seconds or ACCESS_TOKEN_EXPIRE_SECONDS)

    payload: Dict[str, Any] = {"sub": sub, "iat": now, "nbf": now, "exp": exp}
    if JWT_ISSUER:
        payload["iss"] = JWT_ISSUER
    if JWT_AUDIENCE:
        payload["aud"] = JWT_AUDIENCE
    if extra:
        payload.update(extra)

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def create_access_token_for_user(user: User, extra: Optional[Dict[str, Any]] = None) -> str:
    extra_claims = dict(extra or {})
    extra_claims.setdefault("confirmed", bool(user.confirmed))
    extra_claims.setdefault("can_manage_queue", bool(user.can_manage_queue))
    return create_access_token(sub=str(user.employee_id), extra=extra_claims)

def decode_token(token: str) -> Dict[str, Any]:
    """
    ถอดรหัส + ตรวจลายเซ็นและ claims ที่จำเป็น
    - ถ้าตั้ง ISS/AUD ใน .env จะตรวจให้ด้วย
    - python-jose ไม่รองรับ leeway → ไม่ส่ง
    """
    try:
        kwargs: Dict[str, Any] = {"algorithms": [ALGORITHM]}
        if JWT_AUDIENCE:
            kwargs["audience"] = JWT_AUDIENCE
        if JWT_ISSUER:
            kwargs["issuer"] = JWT_ISSUER

        # หมายเหตุ: python-jose จะตรวจ exp/nbf/iat ให้อัตโนมัติ
        return jwt.decode(token, SECRET_KEY, **kwargs)

    except ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except JWTError:
        # รวมเคส SignatureInvalid, JWTClaimsError (iss/aud ไม่ตรง), ฯลฯ
        raise _unauthorized("Invalid token")

# ===================== Dependencies =====================
def get_current_user(
    cred: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """
    ดึงผู้ใช้ปัจจุบันจาก Bearer token (ต้องมี Authorization header)
    """
    if not cred:
        raise _unauthorized()
    payload = decode_token(cred.credentials)
    emp = payload.get("sub")
    if not emp:
        raise _unauthorized("Invalid token payload")

    user = find_user(db, emp)
    if not user:
        raise _unauthorized("User not found")
    return user

def get_confirmed_user(current: User = Depends(get_current_user)) -> User:
    """ใช้กับ endpoint ที่ต้องการให้ผู้ใช้ยืนยันโปรไฟล์แล้วเท่านั้น"""
    if not current.confirmed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please confirm your profile first.",
        )
    return current

def get_manager_user(current: User = Depends(get_current_user)) -> User:
    """ใช้กับ endpoint ที่ต้องการสิทธิ์จัดการคิว (ผู้จัดการ/แอดมิน)"""
    if not current.can_manage_queue:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager permission required.",
        )
    return current

def get_optional_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    ใช้เมื่อ endpoint ไม่บังคับล็อกอิน:
    - มี token ถูกต้อง → คืน User
    - ไม่มี/ไม่ผ่าน → คืน None
    """
    if not cred:
        return None
    try:
        payload = decode_token(cred.credentials)
        emp = payload.get("sub")
        if not emp:
            return None
        return find_user(db, emp)
    except HTTPException:
        return None

# ---------- ยืดหยุ่น: รับ token ได้ทั้ง Header และ ?token= ----------
def get_user_from_header_or_query(
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    token_q: Optional[str] = Query(default=None, alias="token"),
    db: Session = Depends(get_db),
) -> User:
    """
    ใช้ใน endpoint ที่ผู้ใช้บางทีลืมใส่ Authorization header (เช่น SSE/WS)
    รองรับ:
      - Header: Authorization: Bearer <token>
      - Query : ?token=<token>
      - Fallback: ดึงจาก request.query_params (token/access_token/auth)
    """
    # ปกติ: Header / token=
    token = extract_token(cred, token_q)

    # Fallback กันเคส PowerShell/ไลบรารีแปลก ๆ
    if not token:
        qp = request.query_params
        token = qp.get("token") or qp.get("access_token") or qp.get("auth")

    if not token:
        raise _unauthorized()

    payload = decode_token(token)
    emp = payload.get("sub")
    if not emp:
        raise _unauthorized("Invalid token payload")

    user = find_user(db, emp)
    if not user:
        raise _unauthorized("User not found")
    return user

# (Alias ให้เข้ากับชื่อเดิมในโปรเจกต์)
get_user_from_anywhere = get_user_from_header_or_query

# ========== เพิ่มด้านบน: โหลดค่าจาก .env ==========
REFRESH_SECRET_KEY = os.getenv("REFRESH_SECRET_KEY", None)
REFRESH_TOKEN_EXPIRE_SECONDS = int(os.getenv("REFRESH_TOKEN_EXPIRE_SECONDS", "604800"))

# ========== ฟังก์ชัน Refresh Token ==========
def create_refresh_token(sub: str, extra: Optional[Dict[str, Any]] = None) -> str:
    if not REFRESH_SECRET_KEY:
        # เผื่อยังไม่ตั้งค่า ก็ออก token ชนิดเดียวกับ access ไปก่อน (dev only)
        return create_access_token(sub=sub, extra=extra, expires_seconds=REFRESH_TOKEN_EXPIRE_SECONDS)
    now = int(time.time())
    exp = now + REFRESH_TOKEN_EXPIRE_SECONDS
    payload: Dict[str, Any] = {"sub": sub, "iat": now, "nbf": now, "exp": exp, "typ": "refresh"}
    if extra: payload.update(extra)
    # refresh ไม่จำเป็นต้องใส่ iss/aud (ลดโอกาส verify พลาดระหว่างแอป)
    return jwt.encode(payload, REFRESH_SECRET_KEY, algorithm=ALGORITHM)

def decode_refresh_token(token: str) -> Dict[str, Any]:
    """
    ถอดรหัส refresh token
    - ใช้ REFRESH_SECRET_KEY คนละค่ากับ access token
    - ไม่ตรวจ iss/aud เพื่อลดความเสี่ยง verify พลาดใน dev
    """
    try:
        return jwt.decode(token, REFRESH_SECRET_KEY or SECRET_KEY, algorithms=[ALGORITHM])
    except ExpiredSignatureError:
        raise _unauthorized("Refresh token expired")
    except JWTError:
        raise _unauthorized("Invalid refresh token")
