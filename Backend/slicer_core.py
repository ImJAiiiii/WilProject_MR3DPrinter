# backend/slicer_core.py
from __future__ import annotations
import os, re, subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# -------- regex เมตาจากคอมเมนต์ G-code --------
TIME_RE    = re.compile(r";\s*estimated printing time(?:\s*\(normal mode\))?\s*=\s*(.+)", re.I)
TIME_SEC   = re.compile(r"^;\s*TIME:\s*(\d+)\s*$", re.I | re.M)
FIL_G_RE   = re.compile(r";\s*(?:total )?filament used \[g]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_MM_RE  = re.compile(r";\s*filament used \[mm]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_CM3_RE = re.compile(r";\s*filament used \[cm3]\s*=\s*([0-9.+-eE]+)", re.I)

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _as_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "": return default
    return str(v).lower() not in ("0", "false", "no")

def _fmt_fill_density(val) -> Optional[str]:
    """รับค่าได้ทั้ง 0.2 / 20 / "20%" → คืนสตริงตามที่ CLI รับได้"""
    if val is None: return None
    s = str(val).strip()
    if s.endswith("%"):
        try:
            n = float(s[:-1]); n = max(0.0, min(100.0, n)); return f"{int(round(n))}%"
        except: return s
    try:
        n = float(s)
    except:
        return s
    if 0.0 <= n <= 1.0:
        # บางคนส่งสัดส่วน 0..1 → ให้ส่งแบบทศนิยมกลับไป
        return f"{n:.3f}"
    n = max(0.0, min(100.0, n))
    return f"{int(round(n))}%"

def build_prusa_cmd(
    stl_path: Path,
    out_gcode: Path,
    print_profile: Optional[str] = None,
    filament_profile: Optional[str] = None,
    printer_profile: Optional[str] = None,
    datadir: Optional[str] = None,
    bundle_path: Optional[str] = None,
    # overrides จากหน้าเว็บ (ทั้งหมดเป็น optional)
    overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    สร้างคำสั่ง PrusaSlicer CLI:
      - โหลด bundle ด้วย --load ถ้ามี
      - ตั้ง printer/print/material profile ชื่อให้ตรง bundle
      - รองรับ override: infill, walls(perimeters), support, layer_height, nozzle
    """
    exe = _env("PRUSA_SLICER_BIN")
    if not exe:
        raise RuntimeError("PRUSA_SLICER_BIN is not set")

    cmd = [exe, "--export-gcode", "--output", str(out_gcode)]

    # ชี้ data dir (เช่น C:\Users\<user>\AppData\Roaming\PrusaSlicer)
    datadir = datadir or _env("PRUSA_DATADIR")
    if datadir:
        cmd += ["--datadir", datadir]

    # โหลด config bundle (ถ้ามี)
    bundle_path = bundle_path or _env("PRUSA_BUNDLE_PATH")
    if bundle_path and Path(bundle_path).exists():
        cmd += ["--load", bundle_path]

    # เลือก preset (รับจากพารามิเตอร์ ถ้าไม่ส่งมา → fallback .env)
    printer_profile  = printer_profile  or _env("PRUSA_PRINTER_PRESET")
    print_profile    = print_profile    or _env("PRUSA_PRINT_PRESET")
    filament_profile = filament_profile or _env("PRUSA_FILAMENT_PRESET")

    strict = _as_bool("PRUSA_STRICT_PRESET", False)
    if strict and (not printer_profile or not print_profile or not filament_profile):
        raise RuntimeError("Missing preset names (printer/print/material) while PRUSA_STRICT_PRESET=1")

    if printer_profile:  cmd += ["--printer-profile",  printer_profile]
    if print_profile:    cmd += ["--print-profile",    print_profile]
    if filament_profile: cmd += ["--material-profile", filament_profile]

    # เปิด thumbnail ถ้าต้องการ
    if _as_bool("PRUSA_ENABLE_THUMBNAIL", False):
        cmd += ["--thumbnail=400x300", "--thumbnail=220x124"]

    # overrides
    ov = overrides or {}
    if ov.get("infill") is not None:
        cmd.append(f"--fill-density={_fmt_fill_density(ov.get('infill'))}")
    if ov.get("walls") is not None:
        cmd.append(f"--perimeters={int(max(1, int(ov.get('walls'))))}")
    if ov.get("layer_height") is not None:
        cmd.append(f"--layer-height={float(ov.get('layer_height'))}")
    if ov.get("nozzle") is not None:
        cmd.append(f"--nozzle-diameter={float(ov.get('nozzle'))}")

    sup = (ov.get("support") or "none").lower()
    if sup != "none":
        cmd.append("--support-material")
        if sup == "build_plate_only":
            cmd.append("--support-material-buildplate-only")
        elif sup == "enforcers_only":
            cmd.append("--support-material-enforcers-only")

    cmd += [str(stl_path)]
    return cmd

def run_cmd(cmd: List[str]) -> Tuple[int, str]:
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        return cp.returncode, cp.stdout
    except FileNotFoundError:
        return 127, "Slicer executable not found"
    except Exception as e:
        return 1, f"Error: {e}"

def parse_meta_from_gcode(gcode_path: Path) -> Dict:
    meta: Dict[str, Optional[float] | Optional[str] | Optional[int]] = {
        "total_text": None,
        "estimate_min": None,
        "filament_g": None,
        "filament_mm": None,
        "filament_cm3": None,
    }
    text = gcode_path.read_text(encoding="utf-8", errors="ignore")

    # รูปแบบข้อความเวลา
    m = TIME_RE.search(text)
    if m:
        meta["total_text"] = m.group(1).strip()
    else:
        # Fallback ;TIME:seconds
        ms = TIME_SEC.search(text)
        if ms:
            sec = int(ms.group(1))
            h, r = divmod(sec, 3600)
            m, s = divmod(r, 60)
            if h: meta["total_text"] = f"{h}h {m}m {s}s"
            elif m: meta["total_text"] = f"{m}m {s}s"
            else: meta["total_text"] = f"{s}s"
            meta["estimate_min"] = int(round(sec/60))

    for pat, key in ((FIL_G_RE, "filament_g"), (FIL_MM_RE, "filament_mm"), (FIL_CM3_RE, "filament_cm3")):
        mm = pat.search(text)
        if mm:
            try: meta[key] = float(mm.group(1))
            except: pass

    return meta

def slice_stl_to_gcode(
    stl_path: Path,
    out_dir: Path,
    out_name: Optional[str] = None,
    *,
    # เลือก preset (ถ้าไม่ส่งมา → fallback .env)
    printer_profile: Optional[str] = None,
    print_profile: Optional[str] = None,
    filament_profile: Optional[str] = None,
    bundle_path: Optional[str] = None,
    datadir: Optional[str] = None,
    # overrides จากหน้าเว็บ
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict:
    """
    สั่ง PrusaSlicer แปลง STL → G-code โดย:
      - โหลด bundle ผ่าน --load (ถ้ามี)
      - ใช้ printer/print/material profile ที่กำหนด (หรือจาก .env)
      - รองรับ overrides (infill / walls / support / layer_height / nozzle)
    คืนค่า: { gcode_path, log, total_text, estimate_min, filament_g, filament_mm, filament_cm3 }
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_gcode = out_dir / ((out_name or stl_path.stem) + ".gcode")

    cmd = build_prusa_cmd(
        stl_path=stl_path,
        out_gcode=out_gcode,
        print_profile=print_profile,
        filament_profile=filament_profile,
        printer_profile=printer_profile,
        datadir=datadir,
        bundle_path=bundle_path,
        overrides=overrides,
    )
    rc, log = run_cmd(cmd)
    if rc != 0 or not out_gcode.exists():
        raise RuntimeError(f"Slicing failed (exit={rc}). Log:\n{log}")

    meta = parse_meta_from_gcode(out_gcode)
    return {
        "gcode_path": str(out_gcode.resolve()),
        "log": log,
        **meta,
    }
