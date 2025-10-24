from __future__ import annotations
import os, re, tempfile, subprocess, struct
from pathlib import Path
from typing import Optional, Dict, Tuple

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from auth import get_confirmed_user
from models import User
from s3util import new_staging_key, upload_bytes, presign_get, download_to_file

router = APIRouter(prefix="/api/slicer", tags=["slicer"])

# ==== ENV / CONFIG ============================================================
PRUSA_SLICER_BIN = os.getenv("PRUSA_SLICER_BIN") or os.getenv(
    "PRUSASLICER_EXE",
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
)
PRUSA_DATADIR = os.getenv("PRUSA_DATADIR") or os.getenv(
    "PRUSASLICER_DATADIR",
    os.path.join(os.getenv("APPDATA", r"C:\Users\Public"), "PrusaSlicer"),
)

# ใช้ "ชื่อโปรไฟล์" ให้ตรงกับที่เห็นใน GUI
PRUSA_PRINTER_PRESET  = os.getenv("PRUSA_PRINTER_PRESET",  "")
PRUSA_PRINT_PRESET    = os.getenv("PRUSA_PRINT_PRESET",    "")
PRUSA_FILAMENT_PRESET = os.getenv("PRUSA_FILAMENT_PRESET", "")

PRUSA_ENABLE_THUMBNAIL = os.getenv("PRUSA_ENABLE_THUMBNAIL", "0").lower() not in ("0","false","no")
# strict=1 -> ต้องตั้งชื่อ preset ให้ครบทั้ง 3 ตัว
PRUSA_STRICT_PRESET   = os.getenv("PRUSA_STRICT_PRESET", "1").lower() not in ("0","false","no")
PRUSA_DEBUG_CLI       = os.getenv("PRUSA_DEBUG_CLI", "1").lower() not in ("0","false","no")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ==== SCHEMAS =================================================================
class PreviewIn(BaseModel):
    fileId: str
    originExt: str
    jobName: str
    model: str
    slicing: Optional[Dict] = None  # { infill, walls, support, layer_height, nozzle }

class PreviewOut(BaseModel):
    snapshotUrl: Optional[str] = None
    preview_image_url: Optional[str] = None
    snapshotKey: Optional[str] = None
    gcodeUrl: Optional[str] = None
    printer: Optional[str] = None
    settings: Optional[dict] = None
    result: Optional[dict] = None
    gcodeKey: Optional[str] = None
    originalKey: Optional[str] = None
    gcodeId: Optional[str] = None
    originalFileId: Optional[str] = None
    estimateMin: Optional[int] = None
    isGcode: bool = False

class SliceIn(BaseModel):
    stl_key: str
    out_name: str
    slicing: Optional[Dict] = None
    overwrite: Optional[bool] = False
    model: Optional[str] = None

class SliceOut(BaseModel):
    gcode_key: str
    gcode_url: Optional[str] = None
    estimate_min: Optional[int] = None
    total_text: Optional[str] = None
    filament_g: Optional[float] = None
    first_layer: Optional[str] = None
    applied: Optional[dict] = None

class ThumbnailOut(BaseModel):
    snapshotUrl: Optional[str] = None
    url: Optional[str] = None
    snapshotKey: Optional[str] = None
    mime: Optional[str] = None

# ==== REGEX/HELPERS ===========================================================
# เวลา (รับทั้ง '=' และ ':')
TIME_ANY  = re.compile(r';\s*estimated printing time(?:s)?(?:\s*\((normal|silent)\s*mode\))?\s*[:=]\s*([^\r\n]+)', re.I)
# รองรับแบบรายบรรทัด (สไตล์ Cura)
TIME_SEC  = re.compile(r'^;\s*TIME:\s*(\d+)\s*$', re.I | re.M)

# Used filament รวม m + g (ตรงกับ GUI)
USED_FIL_COMBO = re.compile(r';\s*Used filament\s*:\s*([0-9.]+)\s*m\s*,\s*([0-9.]+)\s*g', re.I)

# Filament (หลายรูปแบบ)
FIL_G_ANY = re.compile(
    r';\s*(?:total\s+filament\s+used|filament\s+used|used filament|estimated_filament_weight)\s*'
    r'(?:\[\s*g\s*\]|\(g\)|g)?\s*[:=]\s*([0-9.]+)',
    re.I,
)
FIL_MM    = re.compile(r';\s*(?:filament(?:_used)?\s*\[mm\]|filament_mm|used filament\s*\(m\))\s*[:=]\s*([0-9.]+)', re.I)
FIL_MM3   = re.compile(r';\s*(?:filament used\s*\[(?:mm3|mm\^3|cm3|cm\^3)\]|filament_volume)\s*[:=]\s*([0-9.]+)', re.I)
DENSITY   = re.compile(r';\s*(?:filament(?:_density| density))(?:_g_cm3)?(?:\s*\[g/cm3\])?\s*[:=]\s*([0-9.]+)', re.I)
DIAMETER  = re.compile(r';\s*(?:filament(?:_diameter| diameter))(?:_mm)?(?:\s*\[mm\])?\s*[:=]\s*([0-9.]+)', re.I)

# First-layer: แยก "time" กับ "height"
FIRST_TIME = re.compile(
    r';\s*(?:estimated first layer printing time(?:\s*\((?:normal|silent)\s*mode\))?\s*[:=]\s*([^\r\n]+)'
    r'|first_layer_print_time\s*[:=]\s*([^\r\n]+)'
    r'|first\s*layer\s*time\s*[:=]\s*([^\r\n]+))',
    re.I,
)
FIRST_HEIGHT = re.compile(r';\s*(?:first_layer_height|first\s*layer\s*height)\s*[:=]\s*([^\r\n]+)', re.I)

# ค่า applied ใน header
AP_FILL   = re.compile(r';\s*fill_density\s*=\s*([0-9.]+%?)', re.I)
AP_WALLS  = re.compile(r';\s*perimeters\s*=\s*([0-9]+)', re.I)
AP_SUP    = re.compile(r';\s*support_material\s*=\s*([01])', re.I)
AP_SUP_BP = re.compile(r';\s*support_material_buildplate_only\s*=\s*([01])', re.I)
AP_SUP_EN = re.compile(r';\s*support_material_enforcers_only\s*=\s*([01])', re.I)


def _parse_min(txt: Optional[str]) -> Optional[int]:
    if not txt:
        return None
    h = re.search(r'(\d+)\s*h', txt, re.I)
    m = re.search(r'(\d+)\s*m', txt, re.I)
    s = re.search(r'(\d+)\s*s', txt, re.I)
    sec = (int(h.group(1))*3600 if h else 0) + (int(m.group(1))*60 if m else 0) + (int(s.group(1)) if s else 0)
    if sec:
        return int(round(sec/60))
    if h or m:
        return (int(h.group(1))*60 if h else 0) + (int(m.group(1)) if m else 0)
    return None


def parse_info(gcode_text: str) -> dict:
    info: Dict[str, object] = {}

    # เวลา: เลือก normal mode ก่อน
    matches = TIME_ANY.findall(gcode_text)
    chosen: Optional[str] = None
    for mode, t in matches:
        if (mode or "").lower() == "normal":
            chosen = t.strip()
            break
    if not chosen and matches:
        chosen = matches[0][1].strip()
    if chosen:
        info["total_text"] = chosen
        info["estimate_min"] = _parse_min(chosen)
    else:
        m2 = TIME_SEC.search(gcode_text)
        if m2:
            secs = int(m2.group(1))
            info["estimate_min"] = max(0, round(secs/60))
            h = secs // 3600
            mm = (secs % 3600) // 60
            ss = secs % 60
            info["total_text"] = f"{h}h {mm}m {ss}s" if h else f"{mm}m {ss}s"

    # Used filament (m, g): ใช้ g จากคู่ m,g ก่อน
    m_combo = USED_FIL_COMBO.search(gcode_text)
    if m_combo:
        try:
            g = float(m_combo.group(2))
            if g >= 0:
                info["filament_g"] = round(g, 2)
        except:
            pass

    # ถ้ายังไม่มี g → หารูปแบบอื่น ๆ
    if "filament_g" not in info:
        m = FIL_G_ANY.search(gcode_text)
        if m:
            try:
                g = float(m.group(1))
                if g >= 0:
                    info["filament_g"] = round(g, 2)
            except:
                pass

    # fallback: คิดจาก mm / diameter / density
    if "filament_g" not in info:
        mm = None; mm3 = None; dens = None; dia = None
        x = FIL_MM.search(gcode_text);   mm  = float(x.group(1)) if x and x.group(1) else None
        x = FIL_MM3.search(gcode_text);  mm3 = float(x.group(1)) if x and x.group(1) else None
        x = DENSITY.search(gcode_text);  dens = float(x.group(1)) if x and x.group(1) else None
        x = DIAMETER.search(gcode_text); dia  = float(x.group(1)) if x and x.group(1) else None
        if mm is not None and mm < 20:  # เลขเล็กๆ มักเป็น "เมตร"
            mm = mm * 1000.0
        if mm3 is None and (mm is not None and dia is not None):
            r = dia/2.0
            mm3 = mm * 3.141592653589793 * r * r
        if (mm3 is not None and mm3 > 0) and (dens is not None and dens > 0):
            info["filament_g"] = round((mm3/1000.0) * dens, 2)

    # ---- First layer: เวลาก่อน แล้วค่อยความสูง
    mt = FIRST_TIME.search(gcode_text)
    if mt:
        for g in mt.groups():
            if g:
                fl_txt = g.strip()
                info["first_layer_time_text"] = fl_txt
                info["first_layer_time_min"]  = _parse_min(fl_txt)
                break
    mh = FIRST_HEIGHT.search(gcode_text)
    if mh:
        info["first_layer_height"] = mh.group(1).strip()

    # เพื่อความเข้ากันได้เดิม
    info["first_layer"] = info.get("first_layer_time_text") or info.get("first_layer_height")

    return info


def parse_applied_from_gcode(gcode_text: str) -> dict:
    out = {"fill_density": None, "perimeters": None, "support": None}
    m = AP_FILL.search(gcode_text)
    if m:
        out["fill_density"] = m.group(1).strip()
    m = AP_WALLS.search(gcode_text)
    if m:
        try:
            out["perimeters"] = int(m.group(1))
        except:
            pass
    sup = None
    m_on  = AP_SUP.search(gcode_text)
    m_bp  = AP_SUP_BP.search(gcode_text)
    m_enf = AP_SUP_EN.search(gcode_text)
    if m_on and m_on.group(1) == "1":
        if m_enf and m_enf.group(1) == "1":
            sup = "enforcers_only"
        elif m_bp and m_bp.group(1) == "1":
            sup = "build_plate_only"
        else:
            sup = "everywhere"
    elif m_on and m_on.group(1) == "0":
        sup = "none"
    out["support"] = sup
    return out

# ==== THUMB (keep no-op for compatibility) ====================================
def _qoi_decode_rgba(buf: bytes) -> Tuple[int,int,bytes]:
    if buf[:4] != b'qoif':
        raise ValueError("not qoi")
    w, h, channels, _ = struct.unpack(">IIBB", buf[4:14])
    if channels not in (3, 4):
        raise ValueError("qoi channels")
    stream = memoryview(buf)[14:-8]
    out = bytearray(w * h * 4)
    idx = [(0, 0, 0, 255)] * 64
    r = g = b = 0
    a = 255
    p = 0
    i = 0
    run = 0

    def _hash(r, g, b, a):
        return (r * 3 + g * 5 + b * 7 + a * 11) % 64

    while p < w * h:
        if run:
            run -= 1
        else:
            b0 = stream[i]
            i += 1
            if b0 == 0xFE:
                r, g, b = stream[i], stream[i + 1], stream[i + 2]
                i += 3
            elif b0 == 0xFF:
                r, g, b, a = stream[i], stream[i + 1], stream[i + 2], stream[i + 3]
                i += 4
            else:
                tag = b0 & 0xC0
                if tag == 0x00:
                    r, g, b, a = idx[b0 & 0x3F]
                elif tag == 0x40:
                    r = (r + ((b0 >> 4) & 3) - 2) & 255
                    g = (g + ((b0 >> 2) & 3) - 2) & 255
                    b = (b + (b0 & 3) - 2) & 255
                elif tag == 0x80:
                    b1 = stream[i]
                    i += 1
                    dg = (b0 & 0x3F) - 32
                    dr = ((b1 >> 4) & 15) - 8
                    db = (b1 & 15) - 8
                    r = (r + dg + dr) & 255
                    g = (g + dg) & 255
                    b = (b + dg + db) & 255
                elif tag == 0xC0:
                    run = b0 & 0x3F
        out[p * 4:(p + 1) * 4] = bytes((r, g, b, a))
        idx[_hash(r, g, b, a)] = (r, g, b, a)
        p += 1
    return w, h, bytes(out)


def _png_from_rgba(w: int, h: int, rgba: bytes) -> bytes:
    def _chunk(t, d):
        import struct as _s, zlib as _z
        return _s.pack(">I", len(d)) + t + d + _s.pack(">I", _z.crc32(t + d) & 0xffffffff)

    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b'IHDR', struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b'IDAT', __import__("zlib").compress(bytes(raw), 6))
            + _chunk(b'IEND', b''))

# ==== UTILS ===================================================================
def _ensure_slicer():
    if not os.path.isfile(PRUSA_SLICER_BIN):
        raise HTTPException(500, f"PrusaSlicer not found: {PRUSA_SLICER_BIN}")

def _safe_local_path(upload_dir: str, file_id: str) -> str:
    base = os.path.abspath(upload_dir)
    target = os.path.abspath(os.path.join(base, file_id))
    if not target.startswith(base + os.sep):
        raise HTTPException(400, "Invalid fileId path")
    return target

def _mktemp_path(suffix: str) -> str:
    fd, p = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return p

def _norm(s: str) -> str:
    return re.sub(r'[\W_]+', '', (s or '').lower())

# (เก็บไว้เผื่อใช้ --load .ini ในอนาคต)
def _find_preset_ini(datadir: str, subdir: str, name: str) -> Optional[str]:
    if not datadir or not name:
        return None
    root = os.path.join(datadir, subdir)
    if not os.path.isdir(root):
        return None
    want = _norm(name)
    best = None
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(".ini"):
                continue
            path = os.path.join(dirpath, fn)
            stem = os.path.splitext(fn)[0]
            s1 = _norm(stem)
            score = 0
            if s1 == want:
                score = 100
            elif want in s1:
                score = 80
            if score < 90:
                try:
                    head = open(path, "r", encoding="utf-8", errors="ignore").read(4000)
                    if re.search(r'^\s*name\s*=\s*%s\s*$' % re.escape(name), head, re.M | re.I):
                        score = 90
                except:
                    pass
            if score > 0 and (best is None or score > best[0] or (score == best[0] and len(path) < len(best[1]))):
                best = (score, path)
    return best[1] if best else None

def _preset_load_args(strict: bool) -> list[str]:
    return []  # เราไม่ใช้ --load แล้ว

# =============================== CORE: BUILD CLI ==============================
def _build_cli(src_local: str, out_local: str, slicing: Dict, strict: bool) -> list[str]:
    s = slicing or {}
    support = (s.get("support") or "none").lower()
    fill = s.get("infill")
    walls = s.get("walls")
    layer_h = s.get("layer_height")
    nozzle  = s.get("nozzle")

    cli = [PRUSA_SLICER_BIN, "--export-gcode"]

    # ใช้ Software renderer กันปัญหา OpenGL / access violation
    cli.append("--sw-renderer")

    # ใช้ชื่อโปรไฟล์ + datadir (แนวที่เสถียร)
    if PRUSA_DATADIR:
        cli += ["--datadir", PRUSA_DATADIR]

    if strict and (not PRUSA_PRINTER_PRESET or not PRUSA_PRINT_PRESET or not PRUSA_FILAMENT_PRESET):
        raise HTTPException(500, "Missing preset names (.env): PRUSA_PRINTER_PRESET / PRUSA_PRINT_PRESET / PRUSA_FILAMENT_PRESET")

    if PRUSA_PRINTER_PRESET:
        cli += ["--printer-profile", PRUSA_PRINTER_PRESET]
    if PRUSA_PRINT_PRESET:
        cli += ["--print-profile", PRUSA_PRINT_PRESET]
    if PRUSA_FILAMENT_PRESET:
        cli += ["--material-profile", PRUSA_FILAMENT_PRESET]

    if PRUSA_ENABLE_THUMBNAIL:
        cli += ["--thumbnail=400x300", "--thumbnail=220x124"]

    # overrides จาก FE
    if fill is not None:
        cli.append(f"--fill-density={_fmt_fill_density(fill)}")
    if walls is not None:
        cli.append(f"--perimeters={int(max(1, int(walls)))}")
    if layer_h is not None:
        cli.append(f"--layer-height={float(layer_h)}")
    if nozzle is not None:
        cli.append(f"--nozzle-diameter={float(nozzle)}")

    if support != "none":
        cli.append("--support-material")
        if support == "build_plate_only":
            cli.append("--support-material-buildplate-only")
        elif support == "enforcers_only":
            cli.append("--support-material-enforcers-only")

    cli += ["-o", out_local, src_local]
    return cli

# --- fill-density: 1–100 -> "15%" , 0–1 -> "0.150"
def _fmt_fill_density(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if s.endswith("%"):
        try:
            n = float(s[:-1])
            n = max(0.0, min(100.0, n))
            return f"{int(round(n))}%"
        except:
            return s
    try:
        n = float(s)
    except:
        return s
    if 0.0 <= n <= 1.0:
        return f"{n:.3f}"
    n = max(0.0, min(100.0, n))
    return f"{int(round(n))}%"

def _run_slice(src_local: str, out_local: str, slicing: Dict, strict: bool) -> dict:
    cli = _build_cli(src_local, out_local, slicing, strict)

    if PRUSA_DEBUG_CLI:
        def _q(x):
            x = str(x)
            return f'"{x}"' if (" " in x or "\t" in x) else x
        print("[slicer] CLI =>", " ".join(_q(c) for c in cli))

    try:
        cp = subprocess.run(cli, capture_output=True, text=True, check=True)
        if PRUSA_DEBUG_CLI and cp.stdout:
            print("[slicer] STDOUT =>", cp.stdout[:4000])
        if PRUSA_DEBUG_CLI and cp.stderr:
            print("[slicer] STDERR =>", cp.stderr[:4000])
    except subprocess.CalledProcessError as e:
        if PRUSA_DEBUG_CLI:
            print("[slicer] RC:", e.returncode)
            if e.stdout:
                print("[slicer] STDOUT =>", e.stdout[:4000])
            if e.stderr:
                print("[slicer] STDERR =>", e.stderr[:4000])
        msg = (e.stderr or e.stdout or str(e)).strip()
        raise HTTPException(500, f"PrusaSlicer failed: {msg}")

    s = slicing or {}
    return {
        "fill_density": _fmt_fill_density(s.get("infill")) if s.get("infill") is not None else None,
        "perimeters": int(s["walls"]) if s.get("walls") is not None else None,
        "support": (s.get("support") or "none").lower(),
        "layer_height": float(s["layer_height"]) if s.get("layer_height") is not None else None,
        "nozzle": float(s["nozzle"]) if s.get("nozzle") is not None else None,
        "thumbnail": PRUSA_ENABLE_THUMBNAIL,
        "presets": {
            "printer": PRUSA_PRINTER_PRESET or None,
            "print": PRUSA_PRINT_PRESET or None,
            "filament": PRUSA_FILAMENT_PRESET or None,
        }
    }

def _read_text(p: str) -> str:
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

# ==== /preview ================================================================
@router.post("/preview", response_model=PreviewOut)
def preview(data: PreviewIn, user: User = Depends(get_confirmed_user)):
    origin = (data.originExt or "").lower().strip()
    if not origin:
        raise HTTPException(400, "originExt is required")

    downloaded_tmp = None
    if (data.fileId or "").startswith(("staging/", "storage/")):
        src_local = _mktemp_path(suffix=f".{origin or 'bin'}")
        downloaded_tmp = src_local
        try:
            download_to_file(data.fileId, src_local)
        except Exception as e:
            raise HTTPException(500, f"download source failed: {e}")
        original_key = data.fileId
    else:
        src_local = _safe_local_path(UPLOAD_DIR, data.fileId)
        if not os.path.isfile(src_local):
            raise HTTPException(404, "Uploaded file not found")
        original_key = None

    job_name = data.jobName or Path(src_local).stem

    # ---- GCODE → อ่านเมตาแล้วอัปโหลดเข้า staging เมื่อจำเป็น ----
    if origin in ("gcode", "gco", "gc"):
        try:
            gtxt = _read_text(src_local)
        except Exception:
            gtxt = ""

        gcode_key = data.fileId if (data.fileId or "").startswith(("staging/", "storage/")) \
                    else new_staging_key(f"{job_name}.gcode")
        if gcode_key != data.fileId:
            upload_bytes(gtxt.encode("utf-8", errors="ignore"), gcode_key, content_type="text/x.gcode")

        info = parse_info(gtxt)
        applied_g = parse_applied_from_gcode(gtxt)

        s = data.slicing or {}
        if applied_g.get("fill_density") is None and s.get("infill") is not None:
            applied_g["fill_density"] = _fmt_fill_density(s.get("infill"))
        if applied_g.get("perimeters") is None and s.get("walls") is not None:
            applied_g["perimeters"] = int(s.get("walls"))
        if applied_g.get("support") is None and s.get("support") is not None:
            applied_g["support"] = s.get("support")
        if applied_g.get("layer_height") is None and s.get("layer_height") is not None:
            applied_g["layer_height"] = float(s.get("layer_height"))
        if applied_g.get("nozzle") is None and s.get("nozzle") is not None:
            applied_g["nozzle"] = float(s.get("nozzle"))

        try:
            gcode_url = presign_get(gcode_key)
        except Exception:
            gcode_url = None

        if downloaded_tmp:
            try:
                os.unlink(downloaded_tmp)
            except:
                pass

        return PreviewOut(
            snapshotUrl=None,
            preview_image_url=None,
            snapshotKey=None,
            gcodeUrl=gcode_url,
            printer=PRUSA_PRINTER_PRESET or "PrusaSlicer",
            settings={
                "infill":  s.get("infill"),
                "walls":   s.get("walls"),
                "support": s.get("support", "none"),
                "layer_height": s.get("layer_height"),
                "nozzle":  s.get("nozzle"),
                "model":   data.model,
                "name":    job_name,
            },
            result={
                "total_text":   info.get("total_text"),
                "estimate_min": info.get("estimate_min"),
                "filament_g":   info.get("filament_g"),

                # === First layer (หลายฟอร์แมต) ===
                "first_layer":        info.get("first_layer"),
                "firstLayer":         info.get("first_layer"),
                "first_layer_text":   info.get("first_layer_time_text"),
                "firstLayerText":     info.get("first_layer_time_text"),
                "first_layer_min":    info.get("first_layer_time_min"),
                "firstLayerMin":      info.get("first_layer_time_min"),
                "first_layer_height": info.get("first_layer_height"),
                "firstLayerHeight":   info.get("first_layer_height"),

                "applied":      applied_g,
                "total":        info.get("total_text"),   # FE compat
                "filamentG":    info.get("filament_g"),
            },
            gcodeKey=gcode_key,
            originalKey=original_key or gcode_key,
            gcodeId=gcode_key,
            originalFileId=original_key or gcode_key,
            estimateMin=info.get("estimate_min"),
            isGcode=True,
        )

    # ---- STL/OBJ/3MF/AMF → slice จริง ----
    if origin not in ("stl", "obj", "3mf", "amf"):
        if downloaded_tmp:
            try:
                os.unlink(downloaded_tmp)
            except:
                pass
        raise HTTPException(422, f"Unsupported originExt: {origin}")

    _ensure_slicer()
    out_local = _mktemp_path(suffix=".gcode")

    requested = _run_slice(src_local, out_local, data.slicing or {}, strict=PRUSA_STRICT_PRESET)
    if not os.path.isfile(out_local):
        for p in (src_local, out_local):
            try:
                os.unlink(p)
            except:
                pass
        raise HTTPException(500, "Slicer did not produce G-code")

    try:
        gtxt = _read_text(out_local)
    except Exception:
        gtxt = ""

    gcode_key = new_staging_key(f"{job_name}.gcode")
    upload_bytes(gtxt.encode("utf-8", errors="ignore"), gcode_key, content_type="text/x.gcode")

    info = parse_info(gtxt)
    applied_header = parse_applied_from_gcode(gtxt) or {}
    applied = {**requested, **{k: v for k, v in applied_header.items() if v is not None}}

    try:
        gcode_url = presign_get(gcode_key)
    except Exception:
        gcode_url = None

    for p in (src_local, out_local):
        try:
            os.unlink(p)
        except Exception:
            pass

    return PreviewOut(
        snapshotUrl=None,
        preview_image_url=None,
        snapshotKey=None,
        gcodeUrl=gcode_url,
        printer=(requested.get("presets") or {}).get("printer") or PRUSA_PRINTER_PRESET or "PrusaSlicer",
        settings={
            "infill": (data.slicing or {}).get("infill"),
            "walls":  (data.slicing or {}).get("walls"),
            "support": (data.slicing or {}).get("support") or "none",
            "layer_height": (data.slicing or {}).get("layer_height"),
            "nozzle":  (data.slicing or {}).get("nozzle"),
            "model":  data.model,
            "name":   job_name,
        },
        result={
            "total_text":   info.get("total_text"),
            "estimate_min": info.get("estimate_min"),
            "filament_g":   info.get("filament_g"),

            # === First layer (หลายฟอร์แมต) ===
            "first_layer":        info.get("first_layer"),
            "firstLayer":         info.get("first_layer"),
            "first_layer_text":   info.get("first_layer_time_text"),
            "firstLayerText":     info.get("first_layer_time_text"),
            "first_layer_min":    info.get("first_layer_time_min"),
            "firstLayerMin":      info.get("first_layer_time_min"),
            "first_layer_height": info.get("first_layer_height"),
            "firstLayerHeight":   info.get("first_layer_height"),

            "applied":      applied,
            "total":        info.get("total_text"),   # FE compat
            "filamentG":    info.get("filament_g"),
        },
        gcodeKey=gcode_key,
        originalKey=original_key if original_key else None,
        gcodeId=gcode_key,
        originalFileId=original_key or data.fileId,
        estimateMin=info.get("estimate_min"),
        isGcode=False,
    )

# ==== /slice ==================================================================
@router.post("/slice", response_model=SliceOut)
def slice_endpoint(payload: SliceIn, user: User = Depends(get_confirmed_user)):
    ext = (Path(payload.stl_key).suffix or "").lstrip(".").lower() or "stl"
    if ext not in ("stl", "obj", "3mf", "amf"):
        raise HTTPException(422, "stl_key must point to an STL/OBJ/3MF/AMF object key")

    pv = preview(
        PreviewIn(
            fileId=payload.stl_key,
            originExt=ext,
            jobName=payload.out_name,
            model=payload.model or "",
            slicing=payload.slicing or {},
        ),
        user,
    )
    res = pv.result or {}
    return SliceOut(
        gcode_key=pv.gcodeKey or pv.gcodeId or "",
        gcode_url=pv.gcodeUrl,
        estimate_min=pv.estimateMin or res.get("estimate_min"),
        total_text=res.get("total_text") or res.get("total"),
        filament_g=res.get("filament_g") or res.get("filamentG"),
        first_layer=res.get("first_layer") or res.get("firstLayer"),
        applied=res.get("applied"),
    )

# (คง endpoint /thumbnail ไว้เพื่อ compatibility)
@router.get("/thumbnail", response_model=ThumbnailOut)
def get_thumbnail(object_key: str = Query(..., alias="object_key"),
                  user: User = Depends(get_confirmed_user)):
    if not object_key or not object_key.startswith(("staging/", "storage/")):
        raise HTTPException(400, "object_key must be an S3 key (staging/* or storage/*)")
    return ThumbnailOut()
