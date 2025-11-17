# backend/slicer_prusa.py
from __future__ import annotations
import os, re, tempfile, subprocess, json, base64, logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from slicer_core import slice_stl_to_gcode

from auth import get_confirmed_user
from models import User
from s3util import (
    upload_bytes, presign_get, download_to_file, delete_object,
    staging_triple_keys, commit_triple_to_catalog, catalog_paths_for_job,
)

log = logging.getLogger("slicer")

# ===== Renderers (optional) =====
try:
    from preview_gcode_image import gcode_to_preview_png as _gcode_to_png  # type: ignore
    _HAS_RENDER_NEW = True
except Exception:
    _HAS_RENDER_NEW = False

try:
    from preview import parse_gcode, render_segments_png  # type: ignore
    _HAS_RENDER_OLD = True
except Exception:
    _HAS_RENDER_OLD = False

router = APIRouter(prefix="/api/slicer", tags=["slicer"])

# ==== ENV / CONFIG ============================================================
PRUSA_SLICER_BIN = (
    os.getenv("PRUSA_SLICER_BIN")
    or os.getenv("PRUSA_SLICER_CLI")
    or os.getenv("PRUSASLICER_EXE")
    or r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe"
)
PRUSA_DATADIR = os.getenv("PRUSA_DATADIR") or os.getenv(
    "PRUSASLICER_DATADIR",
    os.path.join(os.getenv("APPDATA", r"C:\Users\Public"), "PrusaSlicer"),
)
PRUSA_BUNDLE_PATH = os.getenv("PRUSA_BUNDLE_PATH", "").strip()

PRUSA_PRINTER_PRESET  = (os.getenv("PRUSA_PRINTER_PRESET",  "") or "").strip()
PRUSA_PRINT_PRESET    = (os.getenv("PRUSA_PRINT_PRESET",    "") or "").strip()
PRUSA_FILAMENT_PRESET = (os.getenv("PRUSA_FILAMENT_PRESET", "") or "").strip()
PRUSA_ALLOW_FE_PRESET = os.getenv("PRUSA_ALLOW_FE_PRESET", "0").lower() not in ("0","false","no")

PRUSA_ENABLE_THUMBNAIL = os.getenv("PRUSA_ENABLE_THUMBNAIL", "0").lower() not in ("0","false","no")
PRUSA_STRICT_PRESET    = os.getenv("PRUSA_STRICT_PRESET", "1").lower() not in ("0","false","no")
PRUSA_DEBUG_CLI        = os.getenv("PRUSA_DEBUG_CLI", "1").lower() not in ("0","false","no")

DELETE_STAGING_AFTER_SLICE = os.getenv("DELETE_STAGING_AFTER_SLICE", "1").lower() not in ("0","false","no")
MIN_VALID_GCODE_BYTES      = int(os.getenv("MIN_VALID_GCODE_BYTES", "50"))

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# === Preview options ===
GEN_PREVIEW   = os.getenv("SLICER_GEN_PREVIEW", "1").lower() not in ("0","false","no")
PREVIEW_SIZE  = os.getenv("SLICER_PREVIEW_SIZE", "3200x2400")
PREVIEW_AZ    = float(os.getenv("SLICER_PREVIEW_AZ", "45"))
PREVIEW_EL    = float(os.getenv("SLICER_PREVIEW_EL", "35.2643897"))
PREVIEW_BED   = os.getenv("SLICER_PREVIEW_BED", "250x220")

# ✅ ใช้ map จาก .env ที่คุณมีอยู่แล้ว (PLA/PETG)
FILAMENT_PRESET_MAP = {
    "PLA":  os.getenv("FILAMENT_PROFILE_PLA",  "").strip(),
    "PETG": os.getenv("FILAMENT_PROFILE_PETG", "").strip(),
}

# ==== Schemas =================================================================
class PreviewIn(BaseModel):
    fileId: str
    originExt: str
    jobName: str
    model: str
    slicing: Optional[Dict] = None

class PreviewOut(BaseModel):
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
    manifestKey: Optional[str] = None
    previewKey: Optional[str] = None

class SliceIn(BaseModel):
    stl_key: str
    out_name: str
    slicing: Optional[Dict] = None
    model: Optional[str] = None

class SliceOut(BaseModel):
    gcode_key: str
    gcode_url: Optional[str] = None
    estimate_min: Optional[int] = None
    total_text: Optional[str] = None
    filament_g: Optional[float] = None
    first_layer: Optional[str] = None
    applied: Optional[dict] = None

# ==== G-code helpers (parse info) =============================================
TIME_ANY  = re.compile(r';\s*estimated printing time(?:s)?(?:\s*\((normal|silent)\s*mode\))?\s*[:=]\s*([^\r\n]+)', re.I)
TIME_SEC  = re.compile(r'^;\s*TIME:\s*(\d+)\s*$', re.I | re.M)
USED_FIL_COMBO = re.compile(r';\s*Used filament\s*:\s*([0-9.]+)\s*m\s*,\s*([0-9.]+)\s*g', re.I)
FIRST_TIME = re.compile(r';\s*(?:estimated first layer printing time|first_layer_print_time|first\s*layer\s*time)\s*[:=]\s*([^\r\n]+)', re.I)
FIRST_HEIGHT = re.compile(r';\s*(?:first_layer_height|first\s*layer\s*height)\s*[:=]\s*([^\r\n]+)', re.I)
AP_FILL   = re.compile(r';\s*fill_density\s*=\s*([0-9.]+%?)', re.I)
AP_WALLS  = re.compile(r';\s*perimeters\s*=\s*([0-9]+)', re.I)
AP_SUP    = re.compile(r';\s*support_material\s*=\s*([01])', re.I)
AP_SUP_BP = re.compile(r';\s*support_material_buildplate_only\s*=\s*([01])', re.I)
AP_SUP_EN = re.compile(r';\s*support_material_enforcers_only\s*=\s*([01])', re.I)

def _parse_min(txt: Optional[str]) -> Optional[int]:
    if not txt: return None
    h = re.search(r'(\d+)\s*h', txt, re.I); m = re.search(r'(\d+)\s*m', txt, re.I); s = re.search(r'(\d+)\s*s', txt, re.I)
    sec = (int(h.group(1))*3600 if h else 0) + (int(m.group(1))*60 if m else 0) + (int(s.group(1)) if s else 0)
    if sec: return int(round(sec/60))
    if h or m: return (int(h.group(1))*60 if h else 0) + (int(m.group(1)) if m else 0)
    return None

def parse_applied_from_gcode(gtxt: str) -> Dict[str, Any]:
    txt = gtxt or ""; out: Dict[str, Any] = {}
    m = AP_FILL.search(txt);   out["fill_density"] = m.group(1) if m else None
    m = AP_WALLS.search(txt);  out["perimeters"]   = int(m.group(1)) if m else None
    sup = AP_SUP.search(txt); bp = AP_SUP_BP.search(txt); en = AP_SUP_EN.search(txt)
    if sup and sup.group(1) == "1":
        mode = "everywhere"
        if bp and bp.group(1) == "1": mode = "build_plate_only"
        if en and en.group(1) == "1": mode = "enforcers_only"
        out["support"] = mode
    else:
        out["support"] = "none"
    fh = FIRST_HEIGHT.search(txt); out["first_layer_height"] = fh.group(1).strip() if fh else None
    return out

def parse_info(gtxt: str) -> Dict[str, Any]:
    txt = gtxt or ""; info: Dict[str, Any] = {}
    m = TIME_ANY.search(txt); ms = TIME_SEC.search(txt) if not m else None
    if m:
        info["total_text"] = m.group(2).strip(); info["estimate_min"] = _parse_min(info["total_text"])
    elif ms:
        sec = int(ms.group(1)); h, r = divmod(sec, 3600); m, s = divmod(r, 60)
        info["total_text"] = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s")
        info["estimate_min"] = int(round(sec/60))
    mg = USED_FIL_COMBO.search(txt) or re.search(r';\s*filament.*?[:=]\s*([0-9.]+)\s*g', txt, re.I)
    if mg: info["filament_g"] = float(mg.group(2) if mg.re is USED_FIL_COMBO else mg.group(1))
    fl = FIRST_TIME.search(txt)
    if fl:
        t = fl.group(1).strip()
        info["first_layer_time_text"] = t
        info["first_layer_time_min"]  = _parse_min(t)
        info["first_layer"]           = t
    fh = FIRST_HEIGHT.search(txt)
    if fh: info["first_layer_height"] = fh.group(1).strip()
    return info

# ==== utilities ===============================================================
def _ensure_slicer():
    if not os.path.isfile(PRUSA_SLICER_BIN):
        raise HTTPException(500, f"PrusaSlicer not found: {PRUSA_SLICER_BIN}")

def _safe_local_path(upload_dir: str, file_id: str) -> str:
    base = os.path.abspath(upload_dir); target = os.path.abspath(os.path.join(base, file_id))
    if not target.startswith(base + os.sep): raise HTTPException(400, "Invalid fileId path")
    return target

def _mktemp_path(suffix: str) -> str:
    fd, p = tempfile.mkstemp(suffix=suffix); os.close(fd); return p

def _fmt_fill_density(val) -> Optional[str]:
    if val is None: return None
    s = str(val).strip()
    if s.endswith("%"):
        try:
            n = float(s[:-1]); n = max(0.0, min(100.0, n)); return f"{int(round(n))}%"
        except Exception:
            return s
    try:
        n = float(s)
    except Exception:
        return s
    if 0.0 <= n <= 1.0: return f"{n:.3f}"
    n = max(0.0, min(100.0, n)); return f"{int(round(n))}%"

def _safe_base(name: str) -> str:
    base = (name or "").strip(); base = re.sub(r"[^\w.\-]+", "_", base)
    return base.strip("_") or "part"

def _dpi_from_size(s: str, default=320) -> int:
    try:
        w, h = [int(t) for t in (s or "").lower().split("x", 1)]
        return int(max(120, min(round(w/8), round(h/6))))
    except Exception:
        return default

def _parse_bed(s: str) -> tuple[float, float]:
    try:
        a, b = (s or "250x220").lower().split("x", 1)
        return float(a), float(b)
    except Exception:
        return 250.0, 220.0

# === Preview-only filtering ====================================================
_TYPE_CANON = {
    "WALL-OUTER": "Perimeter", "WALL OUTER": "Perimeter",
    "PERIMETER": "Perimeter", "EXTERNAL PERIMETER": "External perimeter",
    "WALL-INNER": "Perimeter", "WALL INNER": "Perimeter",
    "TOP SOLID INFILL": "Top solid infill",
    "SOLID INFILL": "Solid infill",
    "INFILL": "Infill",
    "SKIRT/BRIM": "Skirt/Brim", "SKIRT": "Skirt", "BRIM": "Brim",
    "SUPPORT": "Support material", "SUPPORT MATERIAL": "Support material",
}
_PREVIEW_EXCLUDE_TYPES = {"TRAVEL", "RETRACT", "WIPE", "PRIME", "UNKNOWN", "SUPPORT", "SUPPORT MATERIAL"}
_PREVIEW_INCLUDE_CANON = {"Perimeter", "External perimeter", "Solid infill", "Top solid infill", "Infill", "Skirt/Brim", "Skirt", "Brim"}
_TYPE_RE_ANY = re.compile(r";\s*TYPE\s*:\s*([^\r\n]+)", re.I)
_NUM_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)"
_TOK_RE_PREV = re.compile(rf"\b([XYZE])\s*({_NUM_RE})")

def _normalize_and_filter_gcode_for_preview(src_path: str) -> str:
    out_path = _mktemp_path(".gcode")
    curr_type_canon: Optional[str] = "Perimeter"
    last_e = 0.0
    have_e = False

    with open(src_path, "r", errors="ignore") as fi, open(out_path, "w", encoding="utf-8") as fo:
        fo.write("; PREVIEW-ONLY (filtered) — real print G-code remains untouched\n")
        for raw in fi:
            line = raw.strip()

            mtype = _TYPE_RE_ANY.search(line)
            if mtype:
                ty = (mtype.group(1) or "").strip()
                up = ty.upper()
                if up in _PREVIEW_EXCLUDE_TYPES:
                    curr_type_canon = None
                else:
                    curr_type_canon = _TYPE_CANON.get(up, _TYPE_CANON.get(ty, "Perimeter"))
                if curr_type_canon:
                    fo.write(f";TYPE:{curr_type_canon}\n")
                continue

            if not line.startswith(("G0", "G1")):
                continue

            if " Z" in line or line.startswith("G0 Z") or line.startswith("G1 Z"):
                fo.write(line + "\n")
                continue

            coords = dict(_TOK_RE_PREV.findall(line))
            if "E" in coords:
                try:
                    e = float(coords["E"])
                except Exception:
                    continue
                is_extrude = (not have_e) or (e > last_e + 1e-9)
                have_e = True
                last_e = e

                if is_extrude and curr_type_canon in _PREVIEW_INCLUDE_CANON and ("X" in coords and "Y" in coords):
                    fo.write(line + "\n")

    return out_path

# ==== Preview renderers =======================================================
def _render_preview_bytes_from_local_gcode(local_gcode: str) -> Optional[bytes]:
    if not GEN_PREVIEW or not _HAS_RENDER_NEW:
        return None
    try:
        bw, bd = _parse_bed(PREVIEW_BED)
        dpi = _dpi_from_size(PREVIEW_SIZE or "3200x2400", default=320)

        filtered = _normalize_and_filter_gcode_for_preview(local_gcode)

        tmp_png = _mktemp_path(".png")
        try:
            _gcode_to_png(
                filtered, tmp_png,
                include_travel=False,
                fit_mode="object",
                max_obj_fill=0.86,
                bbox_types_for_fit=["EXTERNAL PERIMETER","PERIMETER","WALL-OUTER","WALL-INNER","WALL"],
                pad=0.10, lw=0.70, fade=1.0, zscale=1.0, grid=10.0,
                dpi=dpi, antialias=True, azim_deg=PREVIEW_AZ, elev_deg=PREVIEW_EL,
                bed_w=bw, bed_d=bd,
            )
            with open(tmp_png, "rb") as fp:
                return fp.read()
        finally:
            for p in (tmp_png, filtered):
                try: os.unlink(p)
                except: pass
    except Exception:
        return None

def _render_preview_bytes_from_text(gtxt: str) -> Optional[bytes]:
    if not GEN_PREVIEW or not _HAS_RENDER_OLD: return None
    try:
        try: w, h = (int(s) for s in (PREVIEW_SIZE or "1200x900").lower().split("x", 1))
        except Exception: w, h = 1200, 900
        segs = parse_gcode(gtxt, include_travel=False)
        return render_segments_png(segs, size=(w, h), az=-135.0, el=35.2643897, dist=10.0)
    except Exception:
        return None

def _placeholder_png() -> bytes:
    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/w8AAnsB9d1kBx8AAAAASUVORK5CYII=")

def _build_manifest(job_name: str, gcode_key: str, info: dict, applied: dict | None,
                    original_key: Optional[str], origin_ext: str, presets: dict | None,
                    preview_key: Optional[str]) -> dict:
    return {
        "manifest_version": 1, "name": job_name,
        "gcode_key": gcode_key, "preview_key": preview_key,
        "summary": {
            "estimate_min": info.get("estimate_min"), "total_text": info.get("total_text"),
            "filament_g": info.get("filament_g"), "first_layer": info.get("first_layer"),
            "first_layer_time_text": info.get("first_layer_time_text"),
            "first_layer_time_min":  info.get("first_layer_time_min"),
            "first_layer_height":    info.get("first_layer_height"),
        },
        "applied": applied or {},
        "source": { "original_key": original_key, "origin_ext": origin_ext },
        "slicer": { "presets": presets or {}, "engine": "PrusaSlicer" },
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

# =============================== CLI build/run (preview raw) ===================
def _resolve_presets_from_slicing(s: Dict[str, Any]) -> Dict[str, Optional[str]]:
    prn, prt, mat = PRUSA_PRINTER_PRESET, PRUSA_PRINT_PRESET, PRUSA_FILAMENT_PRESET

    if PRUSA_ALLOW_FE_PRESET:
        prn = (s.get("printer_profile")  or prn or "").strip() or prn
        prt = (s.get("print_profile")    or prt or "").strip() or prt
        mat = (s.get("material_profile") or mat or "").strip() or mat

    # ✅ ไม่แก้ .env ก็ยังได้ filament: ใช้ material จาก FE ถ้ามี, ไม่มีก็ PLA
    material_key = (s.get("material") or "PLA").strip().upper()
    # ถ้ายังไม่มี mat จาก preset เลย → map ตาม material_key / PLA
    if not mat:
        mat = FILAMENT_PRESET_MAP.get(material_key) or FILAMENT_PRESET_MAP.get("PLA") or ""

    if PRUSA_STRICT_PRESET and (not prn or not prt or not mat):
        raise HTTPException(400, "Missing preset names (printer/print/filament).")
    return {"printer": prn or None, "print": prt or None, "filament": mat or None}

def _build_cli(src_local: str, out_local: str, slicing: Dict, strict: bool) -> tuple[List[str], dict]:
    s = slicing or {}; presets = _resolve_presets_from_slicing(s)
    cli = [PRUSA_SLICER_BIN, "--export-gcode", "--sw-renderer"]
    if PRUSA_DATADIR: cli += ["--datadir", PRUSA_DATADIR]
    if PRUSA_BUNDLE_PATH and os.path.exists(PRUSA_BUNDLE_PATH): cli += ["--load", PRUSA_BUNDLE_PATH]
    if presets["printer"]:  cli += ["--printer-profile",   presets["printer"]]
    if presets["print"]:    cli += ["--print-settings",    presets["print"]]       # ✅ สวิตช์ใหม่
    if presets["filament"]: cli += ["--filament-settings", presets["filament"]]    # ✅ สวิตช์ใหม่
    if strict and (not presets["printer"] or not presets["print"] or not presets["filament"]):
        raise HTTPException(500, "Preset names are required (STRICT mode).")

    if (v:=s.get("infill")) is not None:       cli.append(f"--fill-density={_fmt_fill_density(v)}")
    if (v:=s.get("walls")) is not None:        cli.append(f"--perimeters={int(max(1,int(v)))}")
    if (v:=s.get("layer_height")) is not None: cli.append(f"--layer-height={float(v)}")
    if (v:=s.get("nozzle")) is not None:       cli.append(f"--nozzle-diameter={float(v)}")
    sup = (s.get("support") or "none").lower()
    if sup != "none":
        cli.append("--support-material")
        if sup == "build_plate_only": cli.append("--support-material-buildplate-only")
        elif sup == "enforcers_only": cli.append("--support-material-enforcers-only")
    if PRUSA_ENABLE_THUMBNAIL: cli += ["--thumbnail=400x300", "--thumbnail=220x124"]
    cli += ["-o", out_local, src_local]
    return cli, presets

def _try_decode(b: Optional[bytes]) -> str:
    if not b: return ""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try: return b.decode(enc, errors="ignore")
        except Exception: pass
    return b.decode("latin-1", errors="ignore")

def _map_prusa_error_to_user_message(std_err: str, std_out: str) -> str:
    raw = (std_err or "") + "\n" + (std_out or "")
    if "Failed to process the custom G-code template" in raw or "custom G-code" in raw:
        return "PrusaSlicer failed: invalid custom G-code template (check Start/End G-code)."
    if "Parsing error" in raw and "Expecting tag literal-char" in raw:
        return "PrusaSlicer failed: custom G-code uses legacy [ ... ] math. Use { ... } instead."
    if "Referencing a vector variable when scalar is expected" in raw:
        return "PrusaSlicer failed: custom G-code references vector variables; add [0] (e.g., bed_temperature[0])."
    if "unknown preset" in raw.lower():
        return "PrusaSlicer failed: preset not found (check names in bundle/env)."
    return (std_err.strip() or std_out.strip() or "PrusaSlicer failed").splitlines()[0]

def _run_slice(src_local: str, out_local: str, slicing: Dict, strict: bool) -> dict:
    cli, presets_used = _build_cli(src_local, out_local, slicing, strict)

    if PRUSA_DEBUG_CLI:
        def _q(x): x=str(x); return f'"{x}"' if (" " in x or "\t" in x) else x
        log.info("[slicer] CLI => %s", " ".join(_q(c) for c in cli))
        log.info("[slicer] presets => %s", presets_used)

    try:
        cp = subprocess.run(cli, capture_output=True, text=False, check=True)
        out_s = _try_decode(cp.stdout)
        err_s = _try_decode(cp.stderr)
        if PRUSA_DEBUG_CLI and out_s: log.info("[slicer] STDOUT => %s", out_s[:4000])
        if PRUSA_DEBUG_CLI and err_s: log.warning("[slicer] STDERR => %s", err_s[:4000])
    except subprocess.CalledProcessError as e:
        out_s = _try_decode(e.stdout)
        err_s = _try_decode(e.stderr)
        if PRUSA_DEBUG_CLI:
            log.error("[slicer] FAILED rc=%s", e.returncode)
            if out_s: log.info("[slicer] STDOUT => %s", out_s[:4000])
            if err_s: log.warning("[slicer] STDERR => %s", err_s[:4000])
        msg = _map_prusa_error_to_user_message(err_s, out_s)
        raise HTTPException(status_code=500, detail=f"Slicer error: {msg}")

    s = slicing or {}
    return {
        "fill_density": _fmt_fill_density(s.get("infill")) if s.get("infill") is not None else None,
        "perimeters": int(s["walls"]) if s.get("walls") is not None else None,
        "support": (s.get("support") or "none").lower(),
        "layer_height": float(s["layer_height"]) if s.get("layer_height") is not None else None,
        "nozzle": float(s["nozzle"]) if s.get("nozzle") is not None else None,
        "thumbnail": PRUSA_ENABLE_THUMBNAIL,
        "presets": {
            "printer": presets_used.get("printer"),
            "print": presets_used.get("print"),
            "filament": presets_used.get("filament"),
        }
    }

def _read_text(p: str) -> str:
    with open(p, "r", encoding="utf-8", errors="ignore") as f: return f.read()

# ==== /preview ================================================================
@router.post("/preview", response_model=PreviewOut)
def preview(data: PreviewIn, user: User = Depends(get_confirmed_user)):
    origin = (data.originExt or "").lower().strip()
    if origin not in ("stl","gcode"): raise HTTPException(422, "originExt must be 'stl' or 'gcode'")

    downloaded_tmp = None
    if (data.fileId or "").startswith(("staging/","storage/")):
        src_local = _mktemp_path(suffix=f".{origin or 'bin'}"); downloaded_tmp = src_local
        try:
            download_to_file(data.fileId, src_local)
        except Exception as e:
            raise HTTPException(500, f"download source failed: {e}")
        original_key = data.fileId
    else:
        src_local = _safe_local_path(UPLOAD_DIR, data.fileId)
        if not os.path.isfile(src_local): raise HTTPException(404, "Uploaded file not found")
        original_key = None

    model = data.model or "Default"
    job_name = _safe_base(data.jobName) or Path(src_local).stem
    paths = catalog_paths_for_job(model, job_name)

    log.info("[slicer] source=%s model=%s name=%s", data.fileId, model, job_name)

    # ===== G-code → แค่สกัด info + ทำพรีวิว/manifest แล้ว commit =====
    if origin == "gcode":
        try: gtxt = _read_text(src_local)
        except Exception: gtxt = ""

        info = parse_info(gtxt)
        applied_g = parse_applied_from_gcode(gtxt)
        s = data.slicing or {}
        if applied_g.get("fill_density") is None and s.get("infill") is not None:   applied_g["fill_density"] = _fmt_fill_density(s.get("infill"))
        if applied_g.get("perimeters")   is None and s.get("walls")  is not None:   applied_g["perimeters"]   = int(s.get("walls"))
        if applied_g.get("support")      is None and s.get("support")is not None:   applied_g["support"]      = s.get("support")
        if applied_g.get("layer_height") is None and s.get("layer_height") is not None: applied_g["layer_height"] = float(s.get("layer_height"))
        if applied_g.get("nozzle")       is None and s.get("nozzle") is not None:   applied_g["nozzle"]       = float(s.get("nozzle"))

        png_bytes = (_render_preview_bytes_from_local_gcode(src_local)
                     or _render_preview_bytes_from_text(gtxt)
                     or _placeholder_png())

        manifest = _build_manifest(
            job_name=job_name, gcode_key=paths["gcode"], info=info, applied=applied_g,
            original_key=original_key or paths["gcode"], origin_ext=origin,
            presets={"printer": PRUSA_PRINTER_PRESET or None, "print": PRUSA_PRINT_PRESET or None,
                     # ✅ ถ้า .env ไม่ได้ตั้ง PRUSA_FILAMENT_PRESET → ใช้ PLA map
                     "filament": PRUSA_FILAMENT_PRESET or FILAMENT_PRESET_MAP.get("PLA") or None},
            preview_key=paths["preview"],
        )
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        tmp = staging_triple_keys(model, job_name)
        upload_bytes(gtxt.encode("utf-8", errors="ignore"), tmp["gcode_tmp"],   content_type="text/x.gcode")
        upload_bytes(manifest_bytes,                          tmp["json_tmp"],  content_type="application/json")
        upload_bytes(png_bytes,                               tmp["preview_tmp"], content_type="image/png")
        final = commit_triple_to_catalog(model, job_name, tmp)

        if DELETE_STAGING_AFTER_SLICE and (data.fileId or "").startswith("staging/"):
            try: delete_object(data.fileId)
            except: pass
        if downloaded_tmp:
            try: os.unlink(downloaded_tmp)
            except: pass

        try: gcode_url = presign_get(final["gcode"])
        except Exception: gcode_url = None

        return PreviewOut(
            gcodeUrl=gcode_url,
            printer=PRUSA_PRINTER_PRESET or "PrusaSlicer",
            settings={"infill": s.get("infill"), "walls": s.get("walls"), "support": s.get("support", "none"),
                      "layer_height": s.get("layer_height"), "nozzle": s.get("nozzle"), "model": model, "name": job_name},
            result={"total_text": info.get("total_text"), "estimate_min": info.get("estimate_min"), "filament_g": info.get("filament_g")},
            gcodeKey=final["gcode"], originalKey=original_key or final["gcode"],
            gcodeId=final["gcode"], originalFileId=original_key or data.fileId,
            estimateMin=info.get("estimate_min"), isGcode=True,
            manifestKey=final["json"], previewKey=final["preview"],
        )

    # ===== STL → slice =====
    _ensure_slicer()
    try:
        result = slice_stl_to_gcode(
            stl_path=Path(src_local),
            out_dir=Path(tempfile.gettempdir()),
            out_name=_safe_base(data.jobName) or Path(src_local).stem,
            printer_profile=(data.slicing or {}).get("printer_profile")  or PRUSA_PRINTER_PRESET  or None,
            print_profile=(data.slicing or {}).get("print_profile")      or PRUSA_PRINT_PRESET    or None,
            # ✅ ถ้าไม่มี filament จาก env → ใช้ PLA จาก map
            filament_profile=(data.slicing or {}).get("material_profile") or PRUSA_FILAMENT_PRESET
                              or FILAMENT_PRESET_MAP.get((data.slicing or {}).get("material","PLA").upper())
                              or FILAMENT_PRESET_MAP.get("PLA") or None,
            bundle_path=PRUSA_BUNDLE_PATH or None,
            datadir=PRUSA_DATADIR or None,
            overrides={
                "infill": (data.slicing or {}).get("infill"),
                "walls": (data.slicing or {}).get("walls"),
                "layer_height": (data.slicing or {}).get("layer_height"),
                "nozzle": (data.slicing or {}).get("nozzle"),
                "support": (data.slicing or {}).get("support") or "none",
            },
        )
    except RuntimeError as e:
        log.error("slice_stl_to_gcode runtime error: %s", e, exc_info=True)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("slice_stl_to_gcode crashed")
        raise HTTPException(500, f"slicing_failed:unknown: {e}")

    out_local = result["gcode_path"]
    try:
        gtxt = _read_text(out_local)
    except Exception:
        gtxt = ""

    if DELETE_STAGING_AFTER_SLICE and (data.fileId or "").startswith("staging/"):
        try: delete_object(data.fileId)
        except: pass

    info = parse_info(gtxt)
    applied_header = parse_applied_from_gcode(gtxt) or {}
    requested = {
        "fill_density": (data.slicing or {}).get("infill"),
        "perimeters": (data.slicing or {}).get("walls"),
        "support": (data.slicing or {}).get("support") or "none",
        "layer_height": (data.slicing or {}).get("layer_height"),
        "nozzle": (data.slicing or {}).get("nozzle"),
        "thumbnail": PRUSA_ENABLE_THUMBNAIL,
        "presets": {
            "printer":  (data.slicing or {}).get("printer_profile")  or PRUSA_PRINTER_PRESET   or None,
            "print":    (data.slicing or {}).get("print_profile")    or PRUSA_PRINT_PRESET     or None,
            "filament": (data.slicing or {}).get("material_profile") or PRUSA_FILAMENT_PRESET
                        or FILAMENT_PRESET_MAP.get((data.slicing or {}).get("material","PLA").upper())
                        or FILAMENT_PRESET_MAP.get("PLA") or None,
        },
    }
    applied = {**requested, **{k: v for k, v in applied_header.items() if v is not None}}

    png_bytes = (_render_preview_bytes_from_local_gcode(out_local)
                 or _render_preview_bytes_from_text(gtxt)
                 or _placeholder_png())

    if len(gtxt.encode("utf-8", errors="ignore")) < MIN_VALID_GCODE_BYTES:
        try: os.unlink(out_local)
        except: pass
        try:
            if downloaded_tmp: os.unlink(downloaded_tmp)
        except: pass
        raise HTTPException(400, "slicing_failed:empty_gcode")

    manifest = _build_manifest(
        job_name=job_name, gcode_key=paths["gcode"], info=info, applied=applied,
        original_key=(data.fileId if (data.fileId or "").startswith(("staging/","storage/")) else None),
        origin_ext="stl", presets=(requested.get("presets") or {}), preview_key=paths["preview"],
    )
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    tmp = staging_triple_keys(model, job_name)
    upload_bytes(gtxt.encode("utf-8", errors="ignore"), tmp["gcode_tmp"],   content_type="text/x.gcode")
    upload_bytes(manifest_bytes,                          tmp["json_tmp"],  content_type="application/json")
    upload_bytes(png_bytes,                               tmp["preview_tmp"], content_type="image/png")
    final = commit_triple_to_catalog(model, job_name, tmp)

    for p in (src_local, out_local):
        try: os.unlink(p)
        except Exception: pass
    if downloaded_tmp:
        try: os.unlink(downloaded_tmp)
        except Exception:
            pass

    try: gcode_url = presign_get(final["gcode"])
    except Exception: gcode_url = None

    return PreviewOut(
        gcodeUrl=gcode_url,
        printer=(requested.get("presets") or {}).get("printer") or PRUSA_PRINTER_PRESET or "PrusaSlicer",
        settings={"infill": (data.slicing or {}).get("infill"),
                  "walls": (data.slicing or {}).get("walls"),
                  "support": (data.slicing or {}).get("support") or "none",
                  "layer_height": (data.slicing or {}).get("layer_height"),
                  "nozzle": (data.slicing or {}).get("nozzle"),
                  "model": model, "name": job_name},
        result={"total_text": info.get("total_text"), "estimate_min": info.get("estimate_min"), "filament_g": info.get("filament_g")},
        gcodeKey=final["gcode"], originalKey=(data.fileId if (data.fileId or "").startswith(("staging/","storage/")) else None),
        gcodeId=final["gcode"], originalFileId=data.fileId,
        estimateMin=info.get("estimate_min"), isGcode=False,
        manifestKey=final["json"], previewKey=final["preview"],
    )

# ==== /slice ==================================================================
@router.post("/slice", response_model=SliceOut)
def slice_endpoint(payload: SliceIn, user: User = Depends(get_confirmed_user)):
    ext = (Path(payload.stl_key).suffix or "").lstrip(".").lower() or "stl"
    if ext != "stl":
        raise HTTPException(422, "stl_key must point to a .stl object key")

    pv = preview(
        PreviewIn(
            fileId=payload.stl_key, originExt="stl",
            jobName=payload.out_name, model=payload.model or "",
            slicing=payload.slicing or {},
        ), user,
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

@router.get("/thumbnail", response_model=dict)
def get_thumbnail(object_key: str = Query(..., alias="object_key"),
                  user: User = Depends(get_confirmed_user)):
    if not object_key or not object_key.startswith(("staging/","storage/")):
        raise HTTPException(400, "object_key must be an S3 key (staging/* or storage/*)")
    return {}
