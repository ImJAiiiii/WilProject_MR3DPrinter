# backend/gcode_meta.py
from __future__ import annotations

import re
from typing import Optional, Dict

from s3util import get_object_range, head_object

# -------- time parsers --------
# 1) Cura/OctoPrint style: ;TIME: 3600
_TIME_SECS_RE = re.compile(r";\s*TIME\s*:\s*(\d+)\s*$", re.IGNORECASE | re.M)

# 2) PrusaSlicer style: ; estimated printing time (normal mode) = 1h 23m 45s
#    รับแบบมี/ไม่มี "(normal mode)" และรับช่องว่างยืดหยุ่น
_EST_HMS_RE = re.compile(
    r";\s*estimated\s+printing\s+time(?:s)?(?:\s*\((?:normal|silent)\s*mode\))?\s*[:=]\s*"
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
    re.IGNORECASE,
)

# (หลีกเลี่ยง) ;TIME_ELAPSED: 123.45 — มักเป็นเวลาที่พิมพ์ไปแล้ว
# _TIME_ELAPSED_RE = re.compile(r";\s*TIME_ELAPSED\s*:\s*([\d.]+)\s*$", re.IGNORECASE | re.M)

# -------- filament parsers --------
# คู่ค่าแบบ GUI: ; Used filament: 12.34 m, 45.67 g
_USED_FIL_COMBO = re.compile(r";\s*Used\s+filament\s*:\s*([0-9.]+)\s*m\s*,\s*([0-9.]+)\s*g", re.I)

# ค่าตรงหน่วย g: ; (total) filament used = 45.67 (g)
_FIL_G_ANY = re.compile(
    r";\s*(?:total\s+filament\s+used|filament\s+used|used\s+filament|estimated_filament_weight)"
    r"(?:\s*\[\s*g\s*\]|\s*\(g\)|\s*g)?\s*[:=]\s*([0-9.]+)",
    re.I,
)

# ความยาวเส้น (mm หรือ m): ; filament_used[mm] = 123456  หรือ ; filament_used (m) = 12.3
_FIL_MM = re.compile(
    r";\s*(?:filament(?:_used)?\s*\[\s*mm\s*\]|filament_mm|filament\s*\(m\)|used\s*filament\s*\(m\))\s*[:=]\s*([0-9.]+)",
    re.I,
)

# ปริมาตร: ; filament used [mm3] = 12345
_FIL_MM3 = re.compile(
    r";\s*(?:filament\s*used\s*\[(?:mm3|mm\^3|cm3|cm\^3)\]|filament_volume)\s*[:=]\s*([0-9.]+)",
    re.I,
)

# ความหนาแน่น: ; filament_density [g/cm3] = 1.24
_DENSITY = re.compile(
    r";\s*filament(?:_density|\s*density)(?:_g_cm3)?(?:\s*\[\s*g\s*/\s*cm3\s*\])?\s*[:=]\s*([0-9.]+)",
    re.I,
)

# เส้นผ่านศูนย์กลาง: ; filament_diameter [mm] = 1.75
_DIAMETER = re.compile(
    r";\s*filament(?:_diameter|\s*diameter)(?:_mm)?(?:\s*\[\s*mm\s*\])?\s*[:=]\s*([0-9.]+)",
    re.I,
)

# -------- helpers --------
def _parse_minutes_from_text(txt: str) -> Optional[int]:
    """อ่านเวลารวมเป็น 'นาที' จากคอมเมนต์ใน G-code"""
    # 1) ;TIME: 3600
    m = _TIME_SECS_RE.search(txt)
    if m:
        secs = int(m.group(1))
        return max(int(round(secs / 60.0)), 0)

    # 2) ; estimated printing time ... = 1h 23m 45s
    m = _EST_HMS_RE.search(txt)
    if m:
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        ss = int(m.group(3) or 0)
        total_min = int(round((h * 3600 + mm * 60 + ss) / 60.0))
        return max(total_min, 0)

    return None


def _time_text_from_text(txt: str) -> Optional[str]:
    """คืนข้อความเวลารวม (human text) ถ้ามี เช่น '1h 23m 45s' หรือ '60m 0s'"""
    # กรณี ;TIME: secs — แปลงเป็นข้อความ
    m = _TIME_SECS_RE.search(txt)
    if m:
        secs = int(m.group(1))
        if secs < 0:
            return None
        h = secs // 3600
        mm = (secs % 3600) // 60
        ss = secs % 60
        return f"{h}h {mm}m {ss}s" if h else f"{mm}m {ss}s"

    # กรณี PrusaSlicer — ดึงข้อความเท่าที่มี
    m = _EST_HMS_RE.search(txt)
    if m:
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        ss = int(m.group(3) or 0)
        if h:
            return f"{h}h {mm}m {ss}s"
        if mm or ss:
            return f"{mm}m {ss}s"
        return "0m 0s"
    return None


def _parse_filament_g_from_text(txt: str) -> Optional[float]:
    """อ่านน้ำหนักเส้นใย (กรัม) จากหลายรูปแบบ หากหาไม่ได้ลองคำนวณจากข้อมูลทางกายภาพ"""
    # 1) แบบคู่ m + g
    m = _USED_FIL_COMBO.search(txt)
    if m:
        try:
            g = float(m.group(2))
            if g >= 0:
                return round(g, 2)
        except Exception:
            pass

    # 2) ค่าตรงหน่วย g
    m = _FIL_G_ANY.search(txt)
    if m:
        try:
            g = float(m.group(1))
            if g >= 0:
                return round(g, 2)
        except Exception:
            pass

    # 3) คำนวณจาก mm/mm3 + density + diameter
    mm = None
    mm3 = None
    dens = None
    dia = None

    x = _FIL_MM.search(txt)
    if x and x.group(1):
        try:
            mm = float(x.group(1))
            # บางไฟล์ใช้หน่วยเมตรแต่ใส่หัว tag แบบ mm — ถ้าน้อยกว่า 20 ให้ตีความเป็น "เมตร"
            if mm < 20:
                mm *= 1000.0
        except Exception:
            mm = None

    x = _FIL_MM3.search(txt)
    if x and x.group(1):
        try:
            mm3 = float(x.group(1))
        except Exception:
            mm3 = None

    x = _DENSITY.search(txt)
    if x and x.group(1):
        try:
            dens = float(x.group(1))  # g/cm3
        except Exception:
            dens = None

    x = _DIAMETER.search(txt)
    if x and x.group(1):
        try:
            dia = float(x.group(1))   # mm
        except Exception:
            dia = None

    # ถ้าไม่มี mm3 แต่มี mm และ dia -> แปลง mm -> mm3 (π r^2 * length)
    if mm3 is None and (mm is not None and dia is not None):
        r = dia / 2.0
        mm3 = mm * 3.141592653589793 * r * r

    # ถ้ามี mm3 และ dens -> g = (mm3 / 1000) * dens   (เพราะ 1 cm3 = 1000 mm3)
    if (mm3 is not None and mm3 > 0) and (dens is not None and dens > 0):
        g = (mm3 / 1000.0) * dens
        return round(g, 2)

    return None


# -------- public APIs (bytes) --------
def minutes_from_gcode_bytes(buf: bytes) -> Optional[int]:
    """รับ bytes (หัว/ท้ายไฟล์) แล้วคืนเวลารวมเป็นนาที (int) ถ้าหาได้"""
    try:
        txt = buf.decode("utf-8", errors="ignore")
    except Exception:
        return None
    return _parse_minutes_from_text(txt)


def meta_from_gcode_bytes(buf: bytes) -> Dict[str, Optional[object]]:
    """
    รับ bytes แล้วคืน:
      {
        "time_min": int|None,
        "time_text": str|None,
        "filament_g": float|None
      }
    """
    try:
        txt = buf.decode("utf-8", errors="ignore")
    except Exception:
        return {"time_min": None, "time_text": None, "filament_g": None}
    return {
        "time_min": _parse_minutes_from_text(txt),
        "time_text": _time_text_from_text(txt),
        "filament_g": _parse_filament_g_from_text(txt),
    }


# -------- public APIs (S3 objects) --------
def minutes_from_gcode_object(object_key: str, max_bytes: int = 256 * 1024) -> Optional[int]:
    """
    อ่าน object แบบระบุช่วง (Range) แล้วลอง parse เวลาจาก header ของ G-code
    - ปกติข้อมูลเวลาอยู่ช่วงต้นไฟล์ จึงอ่านแค่ ~256KB ก็พอ
    - ถ้าไม่พบในหัวไฟล์ จะลองอ่านท้ายไฟล์ต่อ (ไฟล์บางตัวเขียน meta ท้ายไฟล์)
    """
    try:
        head = get_object_range(object_key, start=0, length=max_bytes)
        mins = minutes_from_gcode_bytes(head)
        if mins is not None:
            return mins

        meta = head_object(object_key)
        size = int(meta.get("ContentLength", 0) or 0)
        if size > 0:
            tail_len = min(max_bytes, size)
            tail_start = max(0, size - tail_len)
            tail = get_object_range(object_key, start=tail_start, length=tail_len)
            return minutes_from_gcode_bytes(tail)
    except Exception:
        pass
    return None


def meta_from_gcode_object(object_key: str, max_bytes: int = 256 * 1024) -> Dict[str, Optional[object]]:
    """
    อ่าน object แบบระบุช่วง (Range) แล้วคืนเมตาครบชุด:
      { time_min, time_text, filament_g }
    จะอ่านหัวไฟล์ก่อน แล้วไม่พบจึงค่อยลองท้ายไฟล์
    """
    out = {"time_min": None, "time_text": None, "filament_g": None}
    try:
        head = get_object_range(object_key, start=0, length=max_bytes)
        out = meta_from_gcode_bytes(head)
        if any(v is not None for v in out.values()):
            return out

        meta = head_object(object_key)
        size = int(meta.get("ContentLength", 0) or 0)
        if size > 0:
            tail_len = min(max_bytes, size)
            tail_start = max(0, size - tail_len)
            tail = get_object_range(object_key, start=tail_start, length=tail_len)
            out2 = meta_from_gcode_bytes(tail)
            # เติมเฉพาะคีย์ที่ยังเป็น None
            for k, v in out2.items():
                if out.get(k) is None and v is not None:
                    out[k] = v
    except Exception:
        pass
    return out
