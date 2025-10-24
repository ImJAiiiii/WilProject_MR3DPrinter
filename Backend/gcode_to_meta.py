import re, json, sys, pathlib
from datetime import datetime

def gcode_to_meta(gcode_path, part_id, machine_type="Hontec Delta", owner="user42"):
    meta = {
        "part_id": part_id,
        "part_name": pathlib.Path(gcode_path).stem,
        "machine_type": machine_type,
        "category": "Unknown",
        "version": part_id.split("_")[-1] if "_" in part_id else "V1",
        "description": "",
        "materials": [],
        "recommended": {},
        "files": {
            "gcode": f"catalog/{machine_type.replace(' ','_')}/{part_id}/part.gcode",
            "stl": f"catalog/{machine_type.replace(' ','_')}/{part_id}/part.stl",
            "preview_png": f"catalog/{machine_type.replace(' ','_')}/{part_id}/preview_220x124.png"
        },
        "tags": [],
        "owner": owner,
        "uploaded_at": datetime.utcnow().isoformat() + "Z"
    }

    with open(gcode_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith(";"):
                if "filament used [g]" in line:
                    meta["filament_g"] = float(re.findall(r"[\d\.]+", line)[0])
                elif "filament used [mm]" in line:
                    meta["filament_mm"] = float(re.findall(r"[\d\.]+", line)[0])
                elif "estimated printing time" in line:
                    meta["estimated_time"] = line.split("=")[-1].strip()
                elif "layer_height" in line:
                    meta["recommended"]["layer_height_mm"] = float(re.findall(r"[\d\.]+", line)[0])
                elif "material" in line:
                    meta["materials"].append(line.split("=")[-1].strip())

    return meta

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("ใช้แบบนี้: python gcode_to_meta.py file.gcode HT_BeltBracket_V1")
        sys.exit(1)

    gcode_path, part_id = sys.argv[1], sys.argv[2]
    meta = gcode_to_meta(gcode_path, part_id)
    out_path = pathlib.Path(gcode_path).with_suffix(".meta.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print("Saved meta.json:", out_path)
