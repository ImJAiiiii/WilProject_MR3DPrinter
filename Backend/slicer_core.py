# backend/slicer_core.py
from __future__ import annotations
import os, re, json, tempfile, subprocess, logging, math, itertools
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ============ setup logger ============
log = logging.getLogger("slicer.core")

# ---------- STL fixer (graceful fallback) ----------
try:
    import numpy as np
    import trimesh
    from trimesh.transformations import euler_matrix  # สำหรับหมุน mesh ด้วยมุม euler
    _HAS_TRIMESH = True
except Exception as e:
    _HAS_TRIMESH = False
    np = None
    trimesh = None
    log.warning("trimesh not available: %s", e)

# -------- regex เมตาจากคอมเมนต์ G-code --------
TIME_RE    = re.compile(r";\s*estimated printing time(?:\s*\(normal mode\))?\s*[:=]\s*(.+)", re.I)
TIME_SEC   = re.compile(r"^;\s*TIME:\s*(\d+)\s*$", re.I | re.M)
FIL_G_RE   = re.compile(r";\s*(?:total )?filament used \[g]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_MM_RE  = re.compile(r";\s*filament used \[mm]\s*=\s*([0-9.+-eE]+)", re.I)
FIL_CM3_RE = re.compile(r";\s*filament used \[cm3]\s*=\s*([0-9.+-eE]+)", re.I)

# =========================== ENV helpers ===========================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _as_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "": return default
    return str(v).lower() not in ("0", "false", "no")

def _as_float_env(name: str, default: Optional[float]) -> Optional[float]:
    s = os.getenv(name)
    if not s: return default
    try:
        return float(s)
    except:
        return default

# =========================== utils ===========================
def _fmt_fill_density(val) -> Optional[str]:
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
        return f"{n:.3f}"
    n = max(0.0, min(100.0, n))
    return f"{int(round(n))}%"

# =========================== Bed size (profiles.json / ENV / default) ===========================
DEFAULT_BED_X = 250.0
DEFAULT_BED_Y = 210.0
DEFAULT_BED_Z = 220.0

def _load_bed_from_profiles(printer_profile: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    พยายามอ่านโปรไฟล์เตียงจากไฟล์ JSON (ตั้ง ENV: PRUSA_PROFILES_JSON ถ้าตำแหน่งไม่ใช่ไฟล์ดีฟอลต์)
    ฟอร์แมตคาดหวังประมาณ:
      { "printers": { "<name>": { "bed_x": 250, "bed_y": 210, "bed_z": 220 } } }
    """
    profiles_path = _env("PRUSA_PROFILES_JSON")
    if not profiles_path:
        candidate = Path(__file__).with_name("profiles.json")
        if candidate.exists():
            profiles_path = str(candidate)

    if not profiles_path or not Path(profiles_path).exists() or not printer_profile:
        return None, None, None

    try:
        with open(profiles_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        p = (data.get("printers") or {}).get(printer_profile) or {}
        bx = float(p.get("bed_x")) if p.get("bed_x") is not None else None
        by = float(p.get("bed_y")) if p.get("bed_y") is not None else None
        bz = float(p.get("bed_z")) if p.get("bed_z") is not None else None
        return bx, by, bz
    except Exception as e:
        log.warning("profiles.json load failed: %s", e)
        return None, None, None

def _get_bed_size(printer_profile: Optional[str]) -> Tuple[float, float, float]:
    bx, by, bz = _load_bed_from_profiles(printer_profile)
    bx = bx if bx else _as_float_env("PRUSA_BED_X", None)
    by = by if by else _as_float_env("PRUSA_BED_Y", None)
    bz = bz if bz else _as_float_env("PRUSA_BED_Z", None)
    return (
        bx if bx else DEFAULT_BED_X,
        by if by else DEFAULT_BED_Y,
        bz if bz else DEFAULT_BED_Z,
    )

# =========================== STL fixer helpers ===========================
def _guess_units_scale(max_dim_mm: float) -> float:
    if max_dim_mm > 2000:   return 1/1000.0
    if max_dim_mm > 500:    return 1/10.0
    if max_dim_mm < 2:      return 1000.0
    return 1.0

def _safe_scale_to_bed(size_xyz, bed_xyz):
    sx = (bed_xyz[0]*0.95) / max(size_xyz[0], 1e-6)
    sy = (bed_xyz[1]*0.95) / max(size_xyz[1], 1e-6)
    sz = (bed_xyz[2]*0.95) / max(size_xyz[2], 1e-6)
    return min(sx, sy, sz, 1.0)  # ไม่ขยาย เก็บไว้ย่อเท่านั้น

def _repair_call(mesh, name: str):
    """
    เรียกฟังก์ชันซ่อมเมชให้ทนทานต่อความต่างของเวอร์ชัน:
      1) ลองบนโมดูล trimesh.repair
      2) ลองเมธอดบน mesh
      3) ถ้าไม่มีทั้งคู่ให้ข้าม
    """
    try:
        rep = getattr(trimesh, "repair", None) if trimesh else None
        if rep and hasattr(rep, name):
            getattr(rep, name)(mesh)
            return
    except Exception as e:
        log.debug("repair.%s failed: %s", name, e)

    try:
        if hasattr(mesh, name):
            getattr(mesh, name)()
            return
    except Exception as e:
        log.debug("mesh.%s failed: %s", name, e)

# =========================== Auto-orient (support-aware) ===========================
# เปิด/ปิดฟีเจอร์ auto-orient ด้วย ENV: AUTO_ORIENT_ENABLED=1
AUTO_ORIENT_ENABLED = _as_bool("AUTO_ORIENT_ENABLED", False)
CANDIDATE_ANGLES = (0, 90, 180, 270)

def _auto_orient_mesh_simple(mesh: "trimesh.Trimesh",
                             support_mode: Optional[str] = None) -> "trimesh.Trimesh":
    """
    ลองหมุนโมเดลรอบแกน X/Y (0/90/180/270 องศา) แล้วเลือกท่าที่:
      - ความสูงรวม (แกน Z) ต่ำ
      - พื้นที่สัมผัสเตียงเยอะ
      - ถ้า support = none → ลดพื้นที่ผิวที่ห้อยลง (overhang/หันลงล่าง) ให้มากที่สุด
    """
    if not _HAS_TRIMESH:
        return mesh

    no_support = (support_mode or "none").lower() == "none"

    best_mesh = mesh.copy()
    best_score = -1e18

    for rx_deg in CANDIDATE_ANGLES:
        for ry_deg in CANDIDATE_ANGLES:
            m = mesh.copy()

            # หมุนรอบ X/Y ตามมุมที่ลอง
            M = euler_matrix(
                math.radians(rx_deg),
                math.radians(ry_deg),
                0.0,  # ยังไม่หมุนรอบ Z เพราะไม่กระทบ lay-flat เท่าไร
            )
            m.apply_transform(M)

            # เลื่อนให้ฐานลงมาแตะ Z=0
            bbox = m.bounds
            z_min = float(bbox[0][2])
            if z_min != 0.0:
                m.apply_translation([0.0, 0.0, -z_min])

            # ความสูงรวมของโมเดล
            bbox = m.bounds
            height_z = float(bbox[1][2] - bbox[0][2])

            # ประมาณพื้นที่สัมผัสเตียง + พื้นที่ overhang
            try:
                normals = m.face_normals  # (N, 3)
                areas   = m.area_faces    # (N,)
                # ฐานที่หันขึ้นด้านบน (normal Z > 0.9)
                up_faces = normals[:, 2] > 0.9
                contact_area = float(areas[up_faces].sum())
                # ผิวที่หันลงล่าง (normal Z < 0) ถือว่าเสี่ยง overhang
                down_faces = normals[:, 2] < 0.0
                overhang_area = float(areas[down_faces].sum())
            except Exception:
                contact_area = 0.0
                overhang_area = 0.0

            # ถ้าไม่ใช้ support → ลงโทษ overhang แรงขึ้น
            overhang_penalty = (0.05 if no_support else 0.01) * overhang_area

            # ยิ่งเตี้ย + ฐานใหญ่ + overhang น้อย → score สูง
            score = -height_z + 0.01 * contact_area - overhang_penalty

            if score > best_score:
                best_score = score
                best_mesh = m

    return best_mesh

# =========================== STL prepare ===========================
def prepare_stl_for_slicing(src_stl_path: Path,
                            printer_profile: Optional[str],
                            support_mode: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
    """
    โหลด STL -> ซ่อมเมช -> (auto-orient ถ้าเปิดใช้ และรู้ support_mode) -> เดาสเกล
    -> ย่อให้พอดีเตียง -> วาง Z=0 และจัดกึ่งกลาง XY
    คืน (path ไฟล์ STL ชั่วคราวที่พร้อมเข้า Slicer, รายงานผล)
    """
    bed_x, bed_y, bed_z = _get_bed_size(printer_profile)

    # ถ้าไม่มี trimesh ให้ส่งผ่านไฟล์เดิม
    if not _HAS_TRIMESH:
        return Path(src_stl_path), {
            "bed_xyz": (bed_x, bed_y, bed_z),
            "unit_scale": 1.0,
            "fit_scale": 1.0,
            "final_size_xyz": None,
            "note": "trimesh not available; skipped repair/scale/place",
            "support_mode": support_mode,
        }

    mesh = trimesh.load(str(src_stl_path), force='mesh')
    if mesh.is_empty:
        raise RuntimeError("stl_error:empty_mesh")

    # --- ซ่อมเมชแบบทนเวอร์ชัน ---
    _repair_call(mesh, "fix_inversion")
    _repair_call(mesh, "fix_normals")
    _repair_call(mesh, "remove_degenerate_faces")  # เคสที่คุณพัง
    _repair_call(mesh, "fill_holes")

    # --- auto-orient ตาม support mode (ถ้าเปิดใช้ผ่าน ENV) ---
    if AUTO_ORIENT_ENABLED:
        try:
            mesh = _auto_orient_mesh_simple(mesh, support_mode=support_mode)
        except Exception as e:
            log.debug("auto_orient skipped due to error: %s", e)

    # เดาหน่วย + scale
    bbox = mesh.bounds
    size = bbox[1] - bbox[0]
    max_dim = float(np.max(size))
    unit_scale = _guess_units_scale(max_dim)
    try:
        if unit_scale != 1.0:
            mesh.apply_scale(unit_scale)
    except Exception as e:
        log.debug("apply_scale(unit) failed: %s", e)
        unit_scale = 1.0

    # ย่อให้พอดีเตียง (ไม่ขยาย)
    bbox = mesh.bounds
    size = bbox[1] - bbox[0]
    fit_scale = _safe_scale_to_bed(size, (bed_x, bed_y, bed_z))
    try:
        if fit_scale < 1.0:
            mesh.apply_scale(fit_scale)
    except Exception as e:
        log.debug("apply_scale(fit) failed: %s", e)
        fit_scale = 1.0

    # วางลงบนเตียง (Z=0) + จัดให้อยู่กึ่งกลาง XY
    bbox = mesh.bounds
    min_corner, max_corner = bbox
    translate = [
        - (min_corner[0] + max_corner[0]) / 2.0,
        - (min_corner[1] + max_corner[1]) / 2.0,
        -  min_corner[2],
    ]
    try:
        mesh.apply_translation(translate)
    except Exception as e:
        log.debug("apply_translation failed: %s", e)

    # ส่งออกเป็นไฟล์ชั่วคราว
    fd, out_path = tempfile.mkstemp(suffix=".stl")
    os.close(fd)
    out_p = Path(out_path)
    mesh.export(str(out_p))

    report = {
        "bed_xyz": (bed_x, bed_y, bed_z),
        "unit_scale": unit_scale,
        "fit_scale": fit_scale,
        "final_size_xyz": (
            float((mesh.bounds[1]-mesh.bounds[0])[0]),
            float((mesh.bounds[1]-mesh.bounds[0])[1]),
            float((mesh.bounds[1]-mesh.bounds[0])[2]),
        ),
        "support_mode": support_mode,
    }
    return out_p, report

# =========================== Slicer command ===========================
def build_prusa_cmd(
    stl_path: Path,
    out_gcode: Path,
    print_profile: Optional[str] = None,
    filament_profile: Optional[str] = None,
    printer_profile: Optional[str] = None,
    datadir: Optional[str] = None,
    bundle_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    สร้างคำสั่ง PrusaSlicer CLI:
      - โหลด bundle ด้วย --load ถ้ามี
      - ตั้ง printer/print/material profile ชื่อให้ตรง bundle
      - รองรับ override: infill, walls(perimeters), support, layer_height, nozzle
    """
    exe = _env("PRUSA_SLICER_BIN") or _env("PRUSA_SLICER_CLI")
    if not exe:
        raise RuntimeError("PRUSA_SLICER_BIN is not set (or PRUSA_SLICER_CLI).")
    if not Path(exe).exists():
        raise RuntimeError(f"slicing_failed:slicer_not_found ({exe})")

    cmd = [exe, "--export-gcode", "--output", str(out_gcode)]

    if _as_bool("PRUSA_SW_RENDERER", True):
        cmd.append("--sw-renderer")

    datadir = datadir or _env("PRUSA_DATADIR")
    if datadir:
        cmd += ["--datadir", datadir]

    bundle_path = bundle_path or _env("PRUSA_BUNDLE_PATH")
    if bundle_path and Path(bundle_path).exists():
        cmd += ["--load", bundle_path]

    printer_profile  = printer_profile  or _env("PRUSA_PRINTER_PRESET")
    print_profile    = print_profile    or _env("PRUSA_PRINT_PRESET")
    filament_profile = (
        filament_profile
        or _env("PRUSA_FILAMENT_PRESET")
        or _env("FILAMENT_PROFILE_PLA")
    )

    strict = _as_bool("PRUSA_STRICT_PRESET", False)
    if strict and (not printer_profile or not print_profile or not filament_profile):
        raise RuntimeError("Missing preset names (printer/print/filament) while PRUSA_STRICT_PRESET=1")

    # ใช้ชื่อสวิตช์ตามที่เครื่องรองรับ (บางรุ่นยังใช้ --print-profile / --material-profile)
    if printer_profile:  cmd += ["--printer-profile",   printer_profile]
    if print_profile:    cmd += ["--print-profile",     print_profile]
    if filament_profile: cmd += ["--material-profile",  filament_profile]

    if _as_bool("PRUSA_ENABLE_THUMBNAIL", False):
        cmd += ["--thumbnail=400x300", "--thumbnail=220x124"]

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
        return cp.returncode, (cp.stdout or "")
    except FileNotFoundError:
        return 127, "Slicer executable not found"
    except Exception as e:
        return 1, f"Error: {e}"

# =========================== G-code meta ===========================
def parse_meta_from_gcode(gcode_path: Path) -> Dict:
    meta: Dict[str, Optional[float] | Optional[str] | Optional[int]] = {
        "total_text": None,
        "estimate_min": None,
        "filament_g": None,
        "filament_mm": None,
        "filament_cm3": None,
    }
    text = gcode_path.read_text(encoding="utf-8", errors="ignore")

    m = TIME_RE.search(text)
    if m:
        meta["total_text"] = m.group(1).strip()
    else:
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

# =========================== Main entry: STL -> G-code ===========================
def slice_stl_to_gcode(
    stl_path: Path,
    out_dir: Path,
    out_name: Optional[str] = None,
    *,
    printer_profile: Optional[str] = None,
    print_profile: Optional[str] = None,
    filament_profile: Optional[str] = None,
    bundle_path: Optional[str] = None,
    datadir: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict:
    """
    แปลง STL → G-code:
      1) ซ่อม + สเกล + จัดวาง STL ให้พอดีเตียงโดยอัตโนมัติ (รวม auto-orient ถ้าเปิดใช้ และดู support_mode)
      2) เรียก PrusaSlicer CLI ด้วย bundle/presets และ overrides
      3) คืนเมตา (เวลา/ฟิลาเมนต์) จากคอมเมนต์ใน G-code
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_gcode = out_dir / ((out_name or Path(stl_path).stem) + ".gcode")

    # support_mode จาก overrides (ค่าที่ user เลือกในฟอร์ม)
    support_mode = (overrides or {}).get("support")

    # 1) เตรียม STL ให้พร้อมพิมพ์ (รู้ support_mode)
    fixed_stl, prep = prepare_stl_for_slicing(
        Path(stl_path),
        printer_profile,
        support_mode=support_mode,
    )

    # 2) สร้างคำสั่งและเรียก CLI
    cmd = build_prusa_cmd(
        stl_path=fixed_stl,
        out_gcode=out_gcode,
        print_profile=print_profile,
        filament_profile=filament_profile,
        printer_profile=printer_profile,
        datadir=datadir,
        bundle_path=bundle_path,
        overrides=overrides,
    )
    rc, logtxt = run_cmd(cmd)

    # 3) ตรวจผล / map ข้อผิดพลาดให้เข้าใจง่าย
    log_l = (logtxt or "").lower()
    if "outside of the print volume" in log_l or "no outline can be derived" in log_l:
        raise RuntimeError("slicing_failed:outside_build_volume\n" + (logtxt or ""))
    if "unknown preset" in log_l or ("can't find" in log_l and "profile" in log_l):
        raise RuntimeError("slicing_failed:preset_not_found\n" + (logtxt or ""))
    if rc == 127:
        raise RuntimeError("slicing_failed:slicer_not_found")
    if rc != 0:
        raise RuntimeError(f"slicing_failed:cli_exit_{rc}\n" + (logtxt or ""))

    if not out_gcode.exists() or out_gcode.stat().st_size < 50:
        raise RuntimeError("slicing_failed:empty_gcode")

    meta = parse_meta_from_gcode(out_gcode)
    return {
        "gcode_path": str(out_gcode.resolve()),
        "log": logtxt,
        "prep": prep,  # unit_scale / fit_scale / final_size_xyz / bed_xyz / support_mode
        **meta,
    }
