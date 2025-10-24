#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# G-code → Isometric Preview (true scale, 4:3, polylines)
import re
from pathlib import Path
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection
from matplotlib.colors import to_rgba

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
TOK_RE  = re.compile(rf"\b([XYZE])\s*({NUM})")
Z_RE    = re.compile(rf"\bZ\s*({NUM})")
TYPE_RE = re.compile(r";\s*TYPE\s*:\s*([\w /-]+)", re.I)

TYPE_COLORS = {
    "Perimeter":"#ffcc00", "External perimeter":"#ff9900",
    "Solid infill":"#e23e3e", "Top solid infill":"#ff5555",
    "Infill":"#e23e3e", "Skirt/Brim":"#2fbec3", "Skirt":"#2fbec3",
    "Brim":"#2fbec3", "Support material":"#9aa3ff", "default":"#ffcc00"
}

# ---------------------- Parse to POLYLINES ----------------------
def parse_gcode_polylines(path: Path, include_travel=False, retract_tol=-1e-9):
    """
    รวม G1 extrusion ต่อเนื่องให้เป็น polyline ยาว ๆ
    - แตกเส้นเมื่อ: travel, เปลี่ยน TYPE, เปลี่ยน Z, retraction (E ลดลง)
    """
    polylines, cols = [], []
    x=y=z=e=0.0
    last_e = 0.0
    curr_type = "default"
    line_pts = []      # [(x,y,z), (..), ...]
    line_type = curr_type
    line_z = None

    def flush():
        nonlocal line_pts, line_type
        if len(line_pts) >= 2:
            polylines.append(np.array(line_pts, dtype=float))
            cols.append(TYPE_COLORS.get(line_type, TYPE_COLORS["default"]))
        line_pts = []

    with path.open("r", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            mtype = TYPE_RE.search(line)
            if mtype:
                new_type = mtype.group(1)
                if new_type != curr_type:
                    flush()
                    curr_type = new_type
                    line_type = curr_type

            if line.startswith(("G0","G1")):
                mZ = Z_RE.search(line)
                if mZ:
                    nz = float(mZ.group(1))
                    if line_z is not None and abs(nz - line_z) > 1e-9:
                        flush()
                    z = nz
                    line_z = z

                coords = dict(TOK_RE.findall(line))
                has_xy = ("X" in coords) and ("Y" in coords)
                if "E" in coords: e = float(coords["E"])
                is_extrude = ("E" in coords) and (e > last_e + 1e-9)

                if has_xy:
                    nx, ny = float(coords["X"]), float(coords["Y"])
                    if include_travel or is_extrude:
                        # เริ่มเส้นใหม่ถ้า type/z เปลี่ยนไปแล้ว หรือยังไม่มีจุดเริ่ม
                        if not line_pts:
                            line_pts = [(x, y, z), (nx, ny, z)]
                        else:
                            line_pts.append((nx, ny, z))
                    else:
                        flush()
                    x, y = nx, ny

                # retraction → ตัดเส้น
                if ("E" in coords) and (e < last_e + retract_tol):
                    flush()

                last_e = e

    flush()
    return polylines, cols

# ---------------------- Grid (robust) ----------------------
def add_floor_grid(ax, xmin, xmax, ymin, ymax, step=10.0, z0=0.0):
    step = float(step) if step and step > 0 else 10.0
    x0 = np.floor(xmin/step)*step; x1 = np.ceil(xmax/step)*step
    y0 = np.floor(ymin/step)*step; y1 = np.ceil(ymax/step)*step
    if x1 <= x0: x1 = x0 + step
    if y1 <= y0: y1 = y0 + step
    xs = np.arange(x0, x1 + 0.5*step, step)
    ys = np.arange(y0, y1 + 0.5*step, step)
    z = float(z0)
    for x in xs: ax.plot([x,x],[ys[0],ys[-1]],[z,z], lw=0.3, color="#343434")
    for y in ys: ax.plot([xs[0],xs[-1]],[y,y],[z,z], lw=0.3, color="#343434")
    ax.plot([x0,x1,x1,x0,x0],[y0,y0,y1,y1,y0],[z,z,z,z,z], lw=0.6, color="#1f6f72")

# ---------------------- Render ----------------------
def render(
    polylines, cols, outpath,
    fade=1.0, lw=0.6, zscale=1.0,
    pad_factor=0.4, grid_step=10.0,
    dpi=400, figsize=(8.0, 6.0),  # 4:3
    antialias=False
):
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax  = fig.add_subplot(111, projection="3d")

    # Theme
    ax.set_facecolor("#151515"); fig.patch.set_facecolor("#151515")
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.pane.set_visible(False); a.line.set_color((0,0,0,0))

    # zscale (เฉพาะ render)
    if polylines and zscale != 1.0:
        scaled = []
        for pl in polylines:
            p = pl.copy()
            p[:,2] *= zscale
            scaled.append(p)
        polylines = scaled

    if polylines:
        all_pts = np.vstack(polylines)
        X, Y, Z = all_pts[:,0], all_pts[:,1], all_pts[:,2]

        # true-scale limits + padding
        xr, yr, zr = np.ptp(X), np.ptp(Y), np.ptp(Z)
        xr = xr if xr > 1e-9 else 1.0
        yr = yr if yr > 1e-9 else 1.0
        zr = zr if zr > 1e-9 else 1.0
        xpad, ypad, zpad = xr*pad_factor, yr*pad_factor, max(zr*0.1, 0.2)
        xmin, xmax = X.min()-xpad, X.max()+xpad
        ymin, ymax = Y.min()-ypad, Y.max()+ypad
        zmin, zmax = Z.min()-zpad, Z.max()+zpad
        ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax); ax.set_zlim(zmin, zmax)
        try:
            ax.set_box_aspect((xmax-xmin, ymax-ymin, zmax-zmin))
        except Exception:
            pass

        # grid ต่ำกว่าชิ้นงานเล็กน้อยกัน z-fighting
        grid_z = Z.min() - max(0.02*(Z.max()-Z.min()), 0.2)
        add_floor_grid(ax, xmin, xmax, ymin, ymax, step=grid_step, z0=grid_z)

        # คำนวณ alpha ต่อ polyline (ตามความสูงเฉลี่ย)
        z_min, z_max = Z.min(), Z.max(); z_range = max(z_max-z_min, 1e-6)
        mean_z = [np.mean(pl[:,2]) for pl in polylines]
        order  = np.argsort(mean_z)  # วาดจากเตี้ย → สูง
        polylines = [polylines[i] for i in order]
        cols      = [cols[i] for i in order]
        alphas = []
        for mz in mean_z:
            norm = (mz - z_min)/z_range
            a = fade ** (1.0 - norm)
            alphas.append(max(0.25, min(1.0, a)))
        alphas = [alphas[i] for i in order]
        rgba_cols = [to_rgba(c, a) for c, a in zip(cols, alphas)]

        # --- ปรับเส้นบางให้คงที่ตาม DPI (บางจริงในพิกเซล) ---
        dpi_scale = fig.dpi / 100.0
        adj_lw = lw / dpi_scale

        # --- แปลง polylines (Nx3 แต่ละเส้น ยาวไม่เท่ากัน) → segments 2 จุด ---
        segments = []
        seg_colors = []
        for pl, c in zip(polylines, rgba_cols):
            if pl.shape[0] < 2:
                continue
            # แตกเป็นคู่ต่อเนื่อง: (p0,p1), (p1,p2), ...
            for i in range(pl.shape[0] - 1):
                segments.append([pl[i], pl[i+1]])
                seg_colors.append(c)

        # กันเคสไม่มี segment
        if segments:
            lc = Line3DCollection(
                segments, colors=seg_colors,
                linewidths=adj_lw, antialiased=antialias
            )

            ax.add_collection3d(lc)

    # กล้อมุม isometric
    ax.view_init(elev=35.2643897, azim=45)
    ax.set_proj_type("ortho")
    ax.set_xticks([]); ax.set_yticks([]); ax.set_zticks([])

    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    plt.savefig(outpath, pad_inches=0, facecolor=fig.get_facecolor())
    plt.close(fig)

# ---------------------- CLI ----------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("--out", default=None)
    ap.add_argument("--include-travel", action="store_true")
    ap.add_argument("--lw", type=float, default=0.45)
    ap.add_argument("--fade", type=float, default=1.0)
    ap.add_argument("--zscale", type=float, default=1.0)
    ap.add_argument("--pad", type=float, default=0.7)
    ap.add_argument("--grid", type=float, default=10.0)
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--aa", action="store_true", help="enable antialias")
    args = ap.parse_args()

    polylines, cols = parse_gcode_polylines(Path(args.gcode), include_travel=args.include_travel)
    out = Path(args.out or (Path(args.gcode).stem + "_preview.png"))
    render(polylines, cols, out,
           fade=args.fade, lw=args.lw, zscale=args.zscale,
           pad_factor=args.pad, grid_step=args.grid,
           dpi=args.dpi, figsize=(8.0, 6.0),  # 4:3
           antialias=args.aa)
    print("saved:", out)
