# backend/slicer_core.py
from __future__ import annotations
import os, re, subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -------- regex เมตาจากคอมเมนต์ G-code --------
TIME_RE    = re.compile(r";\s*estimated printing time(?:\s*\(normal mode\))?\s*=\s*(.+)", re.I)
FIL_G_RE   = re.compile(r";\s*(?:total )?filament used \[g]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_MM_RE  = re.compile(r";\s*filament used \[mm]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_CM3_RE = re.compile(r";\s*filament used \[cm3]\s*=\s*([0-9.+-eE]+)", re.I)

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def build_prusa_cmd(
    stl_path: Path,
    out_gcode: Path,
    print_profile: Optional[str] = None,
    filament_profile: Optional[str] = None,
    printer_profile: Optional[str] = None,
    datadir: Optional[str] = None,
) -> List[str]:
    exe = _env("PRUSA_SLICER_BIN")
    if not exe:
        raise RuntimeError("PRUSA_SLICER_BIN is not set")

    cmd = [exe, "--slice", "--output", str(out_gcode)]

    # ชี้ data dir (เช่น C:\Users\<user>\AppData\Roaming\PrusaSlicer) หากต้องการ
    datadir = datadir or _env("PRUSA_DATADIR")
    if datadir:
        cmd += ["--datadir", datadir]

    # กำหนด preset (บังคับหรือไม่บังคับตาม .env)
    strict = _env("PRUSA_STRICT_PRESET", "0") != "0"
    if print_profile:    cmd += ["--print-profile", print_profile]
    if filament_profile: cmd += ["--material-profile", filament_profile]
    if printer_profile:  cmd += ["--printer-profile", printer_profile]
    if strict:
        # ใช้ชื่อ preset ตาม .env เท่านั้น (ตัด fallback)
        cmd += ["--no-lift-overrides"]  # ไม่เกี่ยว preset โดยตรง แต่กันพฤติกรรมบางอย่าง

    cmd += ["--", str(stl_path)]
    return cmd

def run_cmd(cmd: List[str]) -> Tuple[int, str]:
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return cp.returncode, cp.stdout
    except FileNotFoundError:
        return 127, "Slicer executable not found"
    except Exception as e:
        return 1, f"Error: {e}"

def parse_meta_from_gcode(gcode_path: Path) -> Dict:
    meta: Dict[str, Optional[float] | Optional[str]] = {
        "total_text": None,
        "filament_g": None,
        "filament_mm": None,
        "filament_cm3": None,
    }
    with gcode_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if meta["total_text"] is None:
                m = TIME_RE.search(line)
                if m: meta["total_text"] = m.group(1).strip()
            if meta["filament_g"] is None:
                m = FIL_G_RE.search(line)
                if m:
                    try: meta["filament_g"] = float(m.group(1))
                    except: pass
            if meta["filament_mm"] is None:
                m = FIL_MM_RE.search(line)
                if m:
                    try: meta["filament_mm"] = float(m.group(1))
                    except: pass
            if meta["filament_cm3"] is None:
                m = FIL_CM3_RE.search(line)
                if m:
                    try: meta["filament_cm3"] = float(m.group(1))
                    except: pass
    return meta

def slice_stl_to_gcode(
    stl_path: Path,
    out_dir: Path,
    out_name: Optional[str] = None,
) -> Dict:
    """
    สั่ง PrusaSlicer แปลง STL → G-code
    คืนค่า: { gcode_path, log, total_text, filament_g, filament_mm, filament_cm3 }
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_gcode = out_dir / ((out_name or stl_path.stem) + ".gcode")

    cmd = build_prusa_cmd(
        stl_path=stl_path,
        out_gcode=out_gcode,
        print_profile=_env("PRUSA_PRINT_PRESET"),
        filament_profile=_env("PRUSA_FILAMENT_PRESET"),
        printer_profile=_env("PRUSA_PRINTER_PRESET"),
        datadir=_env("PRUSA_DATADIR"),
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
