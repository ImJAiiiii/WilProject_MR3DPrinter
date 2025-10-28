# backend/gcode_manifest.py
from __future__ import annotations
"""
Minimal + compatible manifest helper for G-code objects stored on S3/MinIO.

- Keeps the original compact structure (used by custom_storage_s3).
- ALSO writes compatibility fields that match the slicer_prusa manifest:
  manifest_version, generated_at, gcode_key, preview_key, summary{}, slicer{}.
"""

from typing import Any, Dict, Optional, Tuple, Callable, List
from datetime import datetime, timezone
import json
import os
import re

from s3util import head_object, put_object, presign_get

MANIFEST_VERSION = 1

# ---------- constants ----------
_MM_PER_CM = 10.0
_DEF_DIA_MM = 1.75       # typical filament diameter
_DEF_DENSITY = 1.24      # PLA density g/cm^3
_RANGE_BYTES = 131072    # 128 KiB head/tail chunk
_SMALL_FILE_CAP = 5 * 1024 * 1024  # 5 MiB (allow full read if small)

# เพิ่ม: ค่าควบคุมการสแกนทั้งไฟล์แบบ chunk (อ่านทีละส่วน)
_SCAN_CHUNK = 256 * 1024            # 256 KiB ต่อ chunk
_MAX_SCAN_BYTES = 64 * 1024 * 1024  # สแกนสูงสุด 64 MiB ป้องกันไฟล์ใหญ่มาก


# ---------------------------- small utils ----------------------------
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_key_for(gcode_key: str) -> str:
    """storage/delta/part.gcode -> storage/delta/part.json"""
    base, _ = os.path.splitext(gcode_key or "")
    return f"{base}.json"


def presign_manifest_for_gcode(gcode_key: str) -> Optional[str]:
    """Presign GET for the manifest JSON if exists."""
    try:
        mk = manifest_key_for(gcode_key)
        head_object(mk)  # ensure exists
        return presign_get(mk)
    except Exception:
        return None


# -------------------------- filament helpers -------------------------
def _mm_to_grams(len_mm: float, dia_mm: float = _DEF_DIA_MM, density_g_cm3: float = _DEF_DENSITY) -> float:
    if not len_mm or len_mm <= 0:
        return 0.0
    r_cm = (dia_mm / 2.0) / _MM_PER_CM
    area_cm2 = 3.141592653589793 * (r_cm ** 2)
    vol_cm3 = area_cm2 * (len_mm / _MM_PER_CM)
    return vol_cm3 * density_g_cm3


# ---------- time / header parsing ----------
_TIME_PATTERNS = [
    r"estimated\s*printing\s*time.*?=\s*([0-9hms :]+)",   # PrusaSlicer
    r";\s*time\s*=\s*([0-9hms :]+)",
    r";\s*print\s*time\s*=\s*([0-9hms :]+)",
    r";\s*estimated\s*time\s*[:=]\s*([0-9hms :]+)",
]

def _text_time_to_minutes(txt: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Accepts '1h 2m 3s', '12m 32s', '753s', '0h 12m 32s', '1:02:03'
    Returns (minutes_int, normalized_text)
    """
    s = (txt or "").strip().lower().replace("hours", "h").replace("mins", "m").replace("sec", "s")
    s = re.sub(r"\s+", " ", s)
    # form 1: H:M:S
    m = re.match(r"^(\d+):(\d+):(\d+)$", s)
    if m:
        h, mi, se = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        mins = h * 60 + mi + (1 if se >= 30 else 0)
        return mins, f"{h}h {mi}m {se}s" if h else (f"{mi}m {se}s" if mi else f"{se}s")
    # tokens with h/m/s
    h = m_ = s2 = 0
    mh = re.search(r"(\d+)\s*h", s)
    mm = re.search(r"(\d+)\s*m", s)
    ms = re.search(r"(\d+)\s*s", s)
    if mh: h = int(mh.group(1))
    if mm: m_ = int(mm.group(1))
    if ms: s2 = int(ms.group(1))
    if (mh or mm or ms):
        mins = h * 60 + m_ + (1 if s2 >= 30 else 0)
        norm = (f"{h}h " if h else "") + (f"{m_}m " if m_ else "") + (f"{s2}s" if s2 else "")
        return mins, norm.strip() or "0m"
    # plain seconds
    msec = re.match(r"^(\d+)\s*s$", s)
    if msec:
        sec = int(msec.group(1))
        mins = sec // 60 + (1 if (sec % 60) >= 30 else 0)
        return mins, f"{mins}m {sec%60}s"
    return None, None


def _extract_filament_from_comments(txt: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (grams, mm) if found in slicer comments (PrusaSlicer/Cura/Simplify3D).
    Matches patterns like:
      ; filament used = 4.60 g
      ; filament used [g] = 4.60
      ; Filament: 4.60 g
      ; filament_total_g = 4.60
      ; filament used = 1234.5 mm
      ; filament used [mm] = 1234.5
    """
    grams: Optional[float] = None
    mm: Optional[float] = None

    def _to_float(s: str) -> float:
        return float(str(s).replace(",", ""))

    # grams
    gram_patterns = [
        r'filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
        r'filament\s*used\s*\[g\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)',
        r'filament[_\s]*total[_\s]*g[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)',
        r'total\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
        r'filament\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
        r'estimated\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
    ]
    for pat in gram_patterns:
        m = re.search(pat, txt, flags=re.I)
        if m:
            try:
                grams = _to_float(m.group(1)); break
            except Exception:
                continue

    # millimeters
    mm_patterns = [
        r'filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*mm',
        r'filament\s*used\s*\[mm\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)',
        r'filament[_\s]*total[_\s]*mm[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)',
    ]
    for pat in mm_patterns:
        m = re.search(pat, txt, flags=re.I)
        if m:
            try:
                mm = _to_float(m.group(1)); break
            except Exception:
                continue

    return grams, mm


def _estimate_filament_mm_from_E_axis(txt: str) -> float:
    """
    Quick estimation of filament length from E-axis:
    - Supports M82 absolute / M83 relative and G92 (re-zero).
    - Sum only positive extrusion (ΔE > 0).
    """
    abs_mode = True  # default M82
    e_last = 0.0
    total = 0.0
    for line in txt.splitlines():
        s = line.strip()
        if not s or s.startswith(';'):
            continue
        if s.startswith('M82'):
            abs_mode = True; continue
        if s.startswith('M83'):
            abs_mode = False; continue
        if s.startswith('G92'):
            m = re.search(r'\bE(-?\d+(?:\.\d+)?)', s, flags=re.I)
            if m: e_last = float(m.group(1))
            continue
        m = re.search(r'\bE(-?\d+(?:\.\d+)?)', s, flags=re.I)
        if not m:
            continue
        e_cur = float(m.group(1))
        if abs_mode:
            d = e_cur - e_last; e_last = e_cur
        else:
            d = e_cur
        if d > 0:
            total += d
    return total


def _extract_time_and_params(txt: str) -> Dict[str, Any]:
    """
    Parse common PrusaSlicer-style header keys into a dict.
    """
    out: Dict[str, Any] = {}

    # time
    for pat in _TIME_PATTERNS:
        m = re.search(pat, txt, flags=re.I)
        if m:
            mi, norm = _text_time_to_minutes(m.group(1))
            if mi is not None:
                out["estimate_min"] = mi
            if norm:
                out["total_text"] = norm
            break

    # presets
    def _grab(key: str) -> Optional[str]:
        m = re.search(rf"{key}\s*=\s*(.+)", txt, flags=re.I)
        return m.group(1).strip() if m else None

    presets = {
        "printer": _grab("printer_settings_id"),
        "filament": _grab("filament_settings_id"),
        "print": _grab("print_settings_id"),
    }
    presets = {k: v for k, v in presets.items() if v}
    if presets:
        out.setdefault("presets", presets)

    # numeric / categorical params
    def _grab_float(key: str) -> Optional[float]:
        m = re.search(rf"{key}\s*=\s*([0-9.]+)", txt, flags=re.I)
        return float(m.group(1)) if m else None

    def _grab_int(key: str) -> Optional[int]:
        m = re.search(rf"{key}\s*=\s*([0-9]+)", txt, flags=re.I)
        return int(m.group(1)) if m else None

    def _grab_percent(key: str) -> Optional[str]:
        m = re.search(rf"{key}\s*=\s*([0-9.]+)\s*%?", txt, flags=re.I)
        return (m.group(1) + "%") if m else None

    out.setdefault("layer_height", _grab_float("layer_height"))
    out.setdefault("first_layer_height", _grab_float("first_layer_height"))
    out.setdefault("perimeters", _grab_int("perimeters"))
    out.setdefault("fill_density", _grab_percent("infill_density"))
    ms = re.search(r"support_material\s*=\s*([01]|true|false|yes|no)", txt, flags=re.I)
    if ms:
        val = ms.group(1).lower()
        out.setdefault("support", "yes" if val in ("1", "true", "yes") else "no")

    return out


def _enrich_manifest_with_filament(manifest: Dict[str, Any], gcode_text: Optional[str]) -> Dict[str, Any]:
    """
    Enrich manifest with filament_total_mm, filament_total_g, and summary.filament_g.
    Also enrich estimate time & common params if found.
    """
    if not gcode_text:
        manifest.setdefault('summary', {}).setdefault('filament_g', None)
        return manifest

    grams, mm = _extract_filament_from_comments(gcode_text)
    if mm is None:
        try:
            mm = _estimate_filament_mm_from_E_axis(gcode_text) or None
        except Exception:
            mm = None
    if grams is None and mm:
        grams = _mm_to_grams(mm)

    if mm is not None:
        manifest['filament_total_mm'] = round(mm, 2)
    if grams is not None:
        manifest['filament_total_g'] = round(grams, 2)
        summary = manifest.setdefault('summary', {})
        summary['filament_g'] = round(grams, 2)
    else:
        manifest.setdefault('summary', {}).setdefault('filament_g', None)

    # ---- time & params from header ----
    try:
        meta = _extract_time_and_params(gcode_text)
        if meta.get("estimate_min") is not None:
            manifest.setdefault("estimate", {})["minutes"] = meta["estimate_min"]
            manifest.setdefault("summary", {})["estimate_min"] = meta["estimate_min"]
        if meta.get("total_text"):
            manifest.setdefault("estimate", {})["text"] = meta["total_text"]
            manifest.setdefault("summary", {})["total_text"] = meta["total_text"]

        app = manifest.get("applied")
        if not isinstance(app, dict):
            app = {}
        for k in ("fill_density", "perimeters", "support", "layer_height"):
            if meta.get(k) is not None and app.get(k) is None:
                app[k] = meta[k]
        if meta.get("first_layer_height") is not None:
            manifest.setdefault("first_layer", {})["height"] = meta["first_layer_height"]
            manifest.setdefault("summary", {})["first_layer_height"] = meta["first_layer_height"]
        if app:
            manifest["applied"] = app

        if meta.get("presets"):
            sl = manifest.setdefault("slicer", {})
            pres = sl.setdefault("presets", {})
            for k, v in meta["presets"].items():
                pres.setdefault(k, v)
    except Exception:
        pass

    return manifest


def _try_read_text_range(
    key: str,
    start: Optional[int],
    end: Optional[int],
    size_hint: Optional[int] = None,
) -> Optional[str]:
    """
    Best-effort: read a byte range. Our s3util implementations typically use
    (key, start, length) — not (start, end). This function adapts accordingly
    and falls back to full GET for small files.
    """
    # Ensure we know object size
    if size_hint is None:
        try:
            h = head_object(key)
            size_hint = int(h.get("ContentLength") or 0)
        except Exception:
            size_hint = None

    def _call_with_start_len(fn: Callable[..., bytes]) -> Optional[str]:
        try:
            s = int(start or 0)
            if end is not None and end >= s:
                length = int(end - s + 1)
            else:
                if size_hint is not None and size_hint > 0 and size_hint <= _SMALL_FILE_CAP:
                    length = max(0, size_hint - s)
                else:
                    length = _RANGE_BYTES
            if length <= 0:
                return None
            blob = fn(key, start=s, length=int(length))  # expected signature
            if blob:
                return blob.decode("utf-8", errors="ignore")
        except Exception:
            return None
        return None

    # Try known names in s3util that conform to (start, length)
    for name in ("get_object_range", "range_get", "get_range"):
        try:
            from s3util import __dict__ as _s3dict  # type: ignore
            fn = _s3dict.get(name)  # type: ignore
        except Exception:
            fn = None
        if callable(fn):
            txt = _call_with_start_len(fn)  # type: ignore
            if txt:
                return txt

    # Fallback: full read for small files
    if size_hint is not None and size_hint <= _SMALL_FILE_CAP:
        try:
            get_obj = None
            try:
                from s3util import get_object  # type: ignore
                get_obj = get_object
            except Exception:
                pass
            if get_obj:
                blob = get_obj(key)
                if blob:
                    return blob.decode("utf-8", errors="ignore")
        except Exception:
            pass

    return None


# ===== NEW: full-file chunk scanner (ใช้เมื่อยังหา filament_g ไม่เจอ) =====
_FILAMENT_GRAM_PATS = [
    r'filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
    r'filament\s*used\s*\[g\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)',
    r'filament[_\s]*total[_\s]*g[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)',
    r'total\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
    r'filament\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
    r'estimated\s*filament[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*g',
]
_FILAMENT_MM_PATS = [
    r'filament\s*used[^0-9\[]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*mm',
    r'filament\s*used\s*\[mm\]\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)',
    r'filament[_\s]*total[_\s]*mm[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)',
]

def _scan_filament_by_ranges(gcode_key: str, size_hint: Optional[int]) -> Tuple[Optional[float], Optional[float]]:
    """
    อ่านทั้งไฟล์แบบ chunk ผ่าน get_object_range()/range_get()/get_range
    คืนค่า (grams, mm) อย่างน้อยสักตัวหนึ่ง ถ้าพบ
    """
    # หา range function
    range_fn = None
    for name in ("get_object_range", "range_get", "get_range"):
        try:
            from s3util import __dict__ as _s3dict  # type: ignore
            fn = _s3dict.get(name)  # type: ignore
        except Exception:
            fn = None
        if callable(fn):
            range_fn = fn
            break
    if not range_fn:
        return None, None

    # จำกัดปริมาณสแกน
    limit = _MAX_SCAN_BYTES if size_hint is None else min(_MAX_SCAN_BYTES, size_hint)
    grams = None
    mm = None
    offset = 0
    scanned = 0

    pat_g = re.compile("|".join(_FILAMENT_GRAM_PATS), flags=re.I)
    pat_mm = re.compile("|".join(_FILAMENT_MM_PATS), flags=re.I)
    overlap = ""  # กัน pattern ตกขอบ chunk

    while scanned < limit:
        length = min(_SCAN_CHUNK, limit - scanned)
        try:
            blob: bytes = range_fn(gcode_key, start=int(offset), length=int(length))  # type: ignore
            if not blob:
                break
            txt = overlap + blob.decode("utf-8", errors="ignore")
        except Exception:
            break

        if grams is None:
            m = pat_g.search(txt)
            if m:
                for g in m.groups():
                    if g:
                        grams = float(str(g).replace(",", ""))
                        break

        if mm is None:
            m2 = pat_mm.search(txt)
            if m2:
                for g in m2.groups():
                    if g:
                        mm = float(str(g).replace(",", ""))
                        break

        if grams is not None and mm is not None:
            break

        overlap = txt[-1024:]  # keep last 1KB for next join
        offset += length
        scanned += length

    return grams, mm


# -------------------------- build manifest ---------------------------
def build_manifest(
    *,
    gcode_key: str,
    job_name: Optional[str] = None,
    model: Optional[str] = None,
    info: Optional[Dict[str, Any]] = None,     # parsed/estimated info
    applied: Optional[Dict[str, Any]] = None,  # actual slicer params used (may contain "presets")
    preview: Optional[Dict[str, Any]] = None,  # {"image_key": "...", "width":..., "height":..., "content_type": ...}
    extra: Optional[Dict[str, Any]] = None,    # any additional metadata (e.g. {"material": "PLA"})
) -> Dict[str, Any]:
    """
    Build a manifest dictionary with BOTH:
      - the original compact shape (gcode/estimate/filament/first_layer/applied/preview)
      - compatibility fields used by slicer_prusa (manifest_version/summary/slicer/etc.)
    """
    info = info or {}
    applied = applied or {}
    extra = extra or {}

    # infer head info for gcode object
    size = None
    etag = None
    content_type = "text/x.gcode"
    try:
        h = head_object(gcode_key)
        size = int(h.get("ContentLength") or 0)
        etag = h.get("ETag")
        content_type = h.get("ContentType") or content_type
    except Exception:
        pass  # don't fail just because HEAD didn't work

    # normalize commonly used values
    est_minutes = (info.get("estimate_min") or info.get("time_min") or info.get("minutes"))
    est_text = info.get("total_text") or info.get("time_text") or info.get("text")
    fil_g = info.get("filament_g") or info.get("filament_grams")

    first_layer_time_text = info.get("first_layer_time_text") or info.get("first_layer")
    first_layer_time_min = info.get("first_layer_time_min")
    first_layer_height = info.get("first_layer_height")

    # applied params
    applied_out: Dict[str, Any] = {
        "fill_density": applied.get("fill_density") or applied.get("infill") or applied.get("infill_percent"),
        "perimeters": applied.get("perimeters") or applied.get("walls") or applied.get("wall_loops"),
        "support": applied.get("support"),
        "layer_height": applied.get("layer_height"),
        "nozzle": applied.get("nozzle") or applied.get("nozzle_diameter"),
        "presets": applied.get("presets"),  # {"printer": "...", "filament": "...", "print": "..."}
    }
    applied_out = {k: v for k, v in applied_out.items() if v is not None}

    # preview packaging
    preview_key = None
    if preview:
        preview_key = preview.get("image_key") or preview.get("key") or preview.get("preview_key")

    # material may come from info/extra
    material = (info.get("material") or extra.get("material") or extra.get("slicer", {}).get("material"))
    presets = applied.get("presets") or extra.get("slicer", {}).get("presets")

    # ---------------- core (original) shape ----------------
    manifest: Dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "created_at": _utcnow_iso(),
        "name": job_name,
        "model": model,
        "gcode": {"key": gcode_key, "size": size, "etag": etag, "content_type": content_type},
        "estimate": {"minutes": est_minutes, "text": est_text},
        "filament": {"grams": fil_g},
        "first_layer": {"time_text": first_layer_time_text, "time_min": first_layer_time_min, "height": first_layer_height},
        "applied": applied_out or None,
        "preview": (preview if preview else None),
        "extra": (extra or None),
    }

    if not manifest.get("applied"): manifest.pop("applied", None)
    if not manifest.get("preview"): manifest.pop("preview", None)
    if not manifest.get("extra"): manifest.pop("extra", None)

    # ---------------- compatibility fields ----------------
    manifest.update({
        "manifest_version": MANIFEST_VERSION,
        "generated_at": manifest["created_at"],
        "gcode_key": gcode_key,
        "preview_key": preview_key,
        "summary": {
            "estimate_min": est_minutes,
            "total_text": est_text,
            "filament_g": fil_g,
            "first_layer": first_layer_time_text,
            "first_layer_time_text": first_layer_time_text,
            "first_layer_time_min": first_layer_time_min,
            "first_layer_height": first_layer_height,
        },
        "slicer": {
            "engine": "PrusaSlicer",
            "presets": presets or {},
            "material": material,
        },
    })

    return manifest


# --------------------------- write manifest --------------------------
def write_manifest_for_gcode(
    *,
    gcode_key: str,
    job_name: Optional[str],
    model: Optional[str],
    info: Optional[Dict[str, Any]],
    applied: Optional[Dict[str, Any]],
    preview: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    cache_control: Optional[str] = "no-cache",
) -> Dict[str, Any]:
    """
    Build & upload the manifest JSON right next to the G-code file.
    Returns: {"key": <manifest_key>, "url": <presigned_url_or_None>, "bytes": <int>}
    """
    manifest = build_manifest(
        gcode_key=gcode_key,
        job_name=job_name,
        model=model,
        info=info,
        applied=applied,
        preview=preview,
        extra=extra,
    )

    # ---------- enrich from G-code text ----------
    try:
        size_hint = None
        try:
            h = head_object(gcode_key)
            size_hint = int(h.get("ContentLength") or 0)
        except Exception:
            pass

        head_txt = _try_read_text_range(gcode_key, start=0, end=_RANGE_BYTES - 1, size_hint=size_hint) or ""
        tail_start = max(0, (size_hint or 0) - _RANGE_BYTES)
        tail_txt = _try_read_text_range(gcode_key, start=tail_start, end=None, size_hint=size_hint) or ""

        gtxt = (tail_txt if tail_txt else "") + ("\n" + head_txt if head_txt else "")
        if not gtxt and size_hint is not None and size_hint <= _SMALL_FILE_CAP:
            try:
                from s3util import get_object  # type: ignore
                blob = get_object(gcode_key)
                gtxt = blob.decode("utf-8", errors="ignore") if blob else ""
            except Exception:
                gtxt = ""

        if gtxt:
            manifest = _enrich_manifest_with_filament(manifest, gtxt)
        else:
            manifest.setdefault("summary", {}).setdefault("filament_g", manifest.get("filament", {}).get("grams"))

        # NEW: ถ้ายังหาไม่ได้ → สแกนทั้งไฟล์แบบ chunk
        if manifest.get("summary", {}).get("filament_g") is None:
            try:
                grams2, mm2 = _scan_filament_by_ranges(gcode_key, size_hint)
                if grams2 is None and (mm2 is not None):
                    grams2 = _mm_to_grams(mm2)
                if grams2 is not None:
                    manifest["filament_total_g"] = round(grams2, 2)
                    manifest.setdefault("summary", {})["filament_g"] = round(grams2, 2)
                if mm2 is not None:
                    manifest["filament_total_mm"] = round(mm2, 2)
            except Exception:
                pass

    except Exception:
        # keep going even if enrichment fails
        pass

    # ---------- upload ----------
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    mkey = manifest_key_for(gcode_key)

    put_object(
        object_key=mkey,
        data=payload,
        content_type="application/json; charset=utf-8",
        cache_control=cache_control or "no-cache",
    )

    try:
        url = presign_get(mkey)
    except Exception:
        url = None

    return {"key": mkey, "url": url, "bytes": len(payload)}
