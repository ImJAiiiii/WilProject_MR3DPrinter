// src/GcodeWebGLPreview.js
import React, {
  useEffect,
  useRef,
  useState,
  forwardRef,
  useImperativeHandle,
} from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

/** ===== CONFIG ===== */
const MAX_BYTES = 25_000_000;
const RANGE_CHUNK = 2_000_000;
const MAX_SEGMENTS = 700_000;
const Z_LIFT_PER_LYR = 0.003;
const MIN_SEG_LEN = 0.15; // mm – ข้ามเส้น extrusion ที่สั้นมาก เพื่อลดนอยส์

/** ===== Colors / Order ===== */
const COLORS = {
  perimeter: new THREE.Color(0.96, 0.77, 0.26),
  external: new THREE.Color(0.94, 0.55, 0.0),
  overhang: new THREE.Color(0.64, 0.35, 0.82),
  infill: new THREE.Color(0.89, 0.34, 0.18),
  solid: new THREE.Color(0.85, 0.2, 0.2),
  top_solid: new THREE.Color(1.0, 0.56, 0.64),
  bridge: new THREE.Color(0.18, 0.53, 0.94),
  skirt: new THREE.Color(0.0, 0.7, 0.55),
  support: new THREE.Color(0.38, 0.68, 1.0),
  gap: new THREE.Color(0.83, 0.38, 0.71),
  other: new THREE.Color(0.3, 0.8, 0.3),
  travel: new THREE.Color(0.5, 0.5, 0.5),
};
// วาดชั้นในก่อน แล้วค่อยผิว → ผิวจะทับด้านบน
const DRAW_ORDER = [
  "infill",
  "solid",
  "bridge",
  "gap",
  "support",
  "skirt",
  "top_solid",
  "overhang",
  "external",
  "perimeter",
];

/** ===== fetch helpers ===== */
async function fetchGcodeText({ apiBase, objectKey, token }) {
  const auth = token ? { Authorization: `Bearer ${token}` } : undefined;

  // 1) ลอง /files/raw ก่อน
  try {
    const r = await fetch(
      `${apiBase}/files/raw?object_key=${encodeURIComponent(objectKey)}`,
      { headers: auth, credentials: "include" }
    );
    if (r.ok) {
      const txt = await r.text();
      if (txt.length > MAX_BYTES) throw new Error("too-large");
      return txt;
    }
  } catch {
    // fallthrough ไป range
  }

  // 2) ไล่อ่าน /api/storage/range เป็น chunk
  const dec = new TextDecoder("utf-8");
  let offset = 0,
    total = Infinity,
    out = "",
    lastSig = "",
    same = 0;

  for (let i = 0; i < 256 && out.length < MAX_BYTES && offset < total; i++) {
    const url = new URL(`${apiBase}/api/storage/range`);
    url.searchParams.set("object_key", objectKey);
    url.searchParams.set("start", String(offset));
    url.searchParams.set("length", String(RANGE_CHUNK));

    const r = await fetch(url.toString(), {
      headers: auth,
      credentials: "include",
    });
    if (!r.ok) break;

    const cr = r.headers.get("Content-Range");
    if (cr) {
      const m = /bytes\s+(\d+)-(\d+)\/(\d+)/i.exec(cr);
      if (m) {
        const s = +m[1],
          e = +m[2],
          t = +m[3];
        if (Number.isFinite(t)) total = t;
        if (s !== offset) break;
        offset = e + 1;
      }
    }

    const buf = await r.arrayBuffer();
    if (!buf.byteLength) break;
    const chunk = dec.decode(buf);
    out += chunk;

    const sig = chunk.slice(0, 128) + "|" + chunk.slice(-128);
    same = sig === lastSig ? same + 1 : 0;
    lastSig = sig;
    if (same >= 2) break;

    if (!cr) offset += buf.byteLength;
    if (buf.byteLength < RANGE_CHUNK) break;
  }

  if (!out) throw new Error("empty-after-range");
  if (out.length > MAX_BYTES) throw new Error("too-large");
  return out;
}

/** ===== parser ===== */
const NUM_RE = /[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/;
const TOK_RE = new RegExp(`\\b([XYZEF])\\s*(${NUM_RE.source})`, "g");
const TYPE_RE = /;\s*TYPE\s*:\s*([A-Za-z0-9 _-]+)/i;
const Z_RE = new RegExp(`;\\s*Z\\s*:\\s*(${NUM_RE.source})`, "i");
const LAYER_RE = /;\s*LAYER_CHANGE\b/i;
const LAYER_NUM_RE = /^;\s*LAYER:\s*(-?\d+)/i;

function mapTypeKey(t) {
  const s = String(t || "").trim().toLowerCase();
  if (s.includes("top") && s.includes("solid")) return "top_solid";
  if (s.includes("external") && s.includes("perimeter")) return "external";
  if (s.includes("overhang") && s.includes("perimeter")) return "overhang";
  if (s.includes("bridge") && s.includes("infill")) return "bridge";
  if (s.includes("solid") && s.includes("infill")) return "solid";
  if (s.includes("perimeter")) return "perimeter";
  if (s.includes("infill")) return "infill";
  if (s.includes("skirt") || s.includes("brim")) return "skirt";
  if (s.includes("support")) return "support";
  if (s.includes("gap")) return "gap";
  return "other";
}

function parseGcode(text, { last = 3 } = {}) {
  if (!text)
    return {
      extru: {},
      bboxAll: [0, 0, 0, 1, 1, 0],
      bboxModel: [0, 0, 0, 1, 1, 0],
      maxLayer: 0,
      clipped: false,
    };
  if (text.length > MAX_BYTES) throw new Error("too-large");

  const extru = {
    infill: [],
    perimeter: [],
    external: [],
    overhang: [],
    solid: [],
    top_solid: [],
    bridge: [],
    skirt: [],
    support: [],
    gap: [],
    other: [],
    travel: [],
  };

  let x = 0,
    y = 0,
    z = 0,
    e = 0,
    absE = true,
    cur = "perimeter",
    layer = 0;
  let maxLayer = 0,
    seg = 0,
    clipped = false;

  const bboxAll = [
    Infinity,
    Infinity,
    Infinity,
    -Infinity,
    -Infinity,
    -Infinity,
  ];
  const bboxModel = [
    Infinity,
    Infinity,
    Infinity,
    -Infinity,
    -Infinity,
    -Infinity,
  ];
  const centerTypes = new Set([
    "perimeter",
    "external",
    "overhang",
    "infill",
    "solid",
    "top_solid",
    "bridge",
    "gap",
    "support",
  ]);

  const keep = (L) =>
    !Number.isFinite(last) || last <= 0 ? true : L >= maxLayer - last + 1;

  for (const raw of text.split(/\r?\n/)) {
    const s = raw.trim();
    if (!s) continue;

    if (s.startsWith("M82")) absE = true;
    if (s.startsWith("M83")) absE = false;

    const t = s.match(TYPE_RE);
    if (t) {
      cur = mapTypeKey(t[1]);
      continue;
    }

    const zt = s.match(Z_RE);
    if (zt) {
      const v = parseFloat(zt[1]);
      if (Number.isFinite(v)) z = v;
    }

    const ln = s.match(LAYER_NUM_RE);
    if (ln) {
      const n = parseInt(ln[1], 10);
      if (Number.isFinite(n)) layer = Math.max(layer, n);
    }
    if (LAYER_RE.test(s)) layer += 1;

    if (!(s.startsWith("G0") || s.startsWith("G1"))) continue;

    TOK_RE.lastIndex = 0;
    const vals = {};
    for (let m; (m = TOK_RE.exec(s)); ) vals[m[1]] = parseFloat(m[2]);

    const X = Number.isFinite(vals.X) ? vals.X : x;
    const Y = Number.isFinite(vals.Y) ? vals.Y : y;
    const Z = Number.isFinite(vals.Z) ? vals.Z : z;

    let dE = 0;
    if (Number.isFinite(vals.E)) {
      const Eraw = vals.E;
      dE = absE ? Eraw - e : Eraw;
      if (absE) e = Eraw;
    }

    if (X !== x || Y !== y || Z !== z) {
      if (dE > 1e-6) {
        if (layer > maxLayer) maxLayer = layer;

        // bbox รวมทุกอย่าง
        if (x < bboxAll[0]) bboxAll[0] = x;
        if (y < bboxAll[1]) bboxAll[1] = y;
        if (z < bboxAll[2]) bboxAll[2] = z;
        if (X > bboxAll[3]) bboxAll[3] = X;
        if (Y > bboxAll[4]) bboxAll[4] = Y;
        if (Z > bboxAll[5]) bboxAll[5] = Z;

        // bbox ตัวงานจริง (ไม่รวม travel/skirt)
        if (centerTypes.has(cur)) {
          if (x < bboxModel[0]) bboxModel[0] = x;
          if (y < bboxModel[1]) bboxModel[1] = y;
          if (z < bboxModel[2]) bboxModel[2] = z;
          if (X > bboxModel[3]) bboxModel[3] = X;
          if (Y > bboxModel[4]) bboxModel[4] = Y;
          if (Z > bboxModel[5]) bboxModel[5] = Z;
        }

        const len = Math.hypot(X - x, Y - y);
        if (keep(layer) && seg < MAX_SEGMENTS && len >= MIN_SEG_LEN) {
          (extru[cur] || extru.other).push([x, y, z, X, Y, Z, layer]);
          seg++;
        } else if (seg >= MAX_SEGMENTS) {
          clipped = true;
        }
      } else {
        if (keep(layer) && seg < MAX_SEGMENTS) {
          extru.travel.push([x, y, z, X, Y, Z, layer]);
        }
      }
    }

    x = X;
    y = Y;
    z = Z;
    if (seg >= MAX_SEGMENTS) clipped = true;
  }

  const safe = (b) => (Number.isFinite(b[0]) ? b : [0, 0, 0, 1, 1, 0]);
  return {
    extru,
    bboxAll: safe(bboxAll),
    bboxModel: safe(bboxModel),
    maxLayer,
    clipped,
  };
}

/** ===== ribbons ===== */
function makeRibbonGeometry(
  segments,
  widthMM = 0.46,
  zLiftPerLayer = Z_LIFT_PER_LYR,
  extraBias = 0.0
) {
  if (!segments?.length) return null;
  const hw = widthMM * 0.5;
  const pos = new Float32Array(segments.length * 12);
  const idx = new Uint32Array(segments.length * 6);
  let vi = 0,
    ii = 0;

  for (const [a, b, c, d, e, f, L] of segments) {
    const dx = d - a;
    const dy = e - b;
    const n = Math.hypot(dx, dy);
    if (n < 1e-9) continue;

    const px = -(dy / n) * hw;
    const py = (dx / n) * hw;
    const z1 = c + L * zLiftPerLayer + extraBias;
    const z2 = f + L * zLiftPerLayer + extraBias;

    pos.set(
      [a - px, b - py, z1, a + px, b + py, z1, d - px, e - py, z2, d + px, e + py, z2],
      vi
    );
    const base = vi / 3;
    idx.set([base, base + 1, base + 2, base + 2, base + 1, base + 3], ii);
    vi += 12;
    ii += 6;
  }

  if (!vi) return null;
  const g = new THREE.BufferGeometry();
  g.setAttribute(
    "position",
    new THREE.BufferAttribute(pos.subarray(0, vi), 3)
  );
  g.setIndex(new THREE.BufferAttribute(idx.subarray(0, ii), 1));
  return g;
}

/** ===== grid helpers ===== */
function gridSpecFromBBox(bboxModel, { padMM = 8, minSize = 120 } = {}) {
  const [x0, y0, , x1, y1] = bboxModel;
  const size = Math.max(minSize, Math.max(x1 - x0, y1 - y0) + padMM * 2);
  const divisions = Math.max(10, Math.round(size / 10)); // 10mm spacing
  return { size, divisions };
}

/** ===== Component (inner) ===== */
function GcodeWebGLPreviewInner(
  {
    objectKey,
    token,
    apiBase = "",
    last = 3,
    widthMM = 0.46,
    // ถ้าไม่ได้ส่ง hide มา จะตั้งจาก preset ให้เอง
    hide,
    preset = "clean", // "clean" | "full"
    height = 360,
    style = {},
    fitTarget = "model", // "model" | "all"
    fitFactor = 1.04,
    gridPadMM = 8,
    minGridSize = 120,
  },
  ref
) {
  const wrapRef = useRef(null);
  const sceneRef = useRef(null);
  const cameraRef = useRef(null);
  const rendererRef = useRef(null);
  const controlsRef = useRef(null);
  const gridRef = useRef(null);

  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [simplified, setSimplified] = useState(false);

  // ----- token ref เพื่อไม่ให้ effect reload เวลา token refresh -----
  const tokenRef = useRef(token);
  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  // init scene / renderer
  useEffect(() => {
    if (!wrapRef.current || sceneRef.current) return;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xffffff);
    sceneRef.current = scene;

    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10_000);
    camera.up.set(0, 0, 1); // Z-up
    camera.position.set(-200, -220, 180);
    cameraRef.current = camera;

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      preserveDrawingBuffer: true, // ให้ toDataURL ได้
    });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    rendererRef.current = renderer;
    wrapRef.current.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = false;
    controls.maxPolarAngle = Math.PI * 0.49;
    controlsRef.current = controls;

    // render loop
    let raf = 0;
    const loop = () => {
      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(loop);
    };
    loop();

    // resize
    const onResize = () => {
      const el = wrapRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
      renderer.setSize(Math.max(1, r.width), Math.max(1, r.height), false);
      renderer.setPixelRatio(dpr);
      camera.aspect = Math.max(1e-3, r.width / Math.max(1, r.height));
      camera.updateProjectionMatrix();
    };
    onResize();
    const ro = new ResizeObserver(onResize);
    ro.observe(wrapRef.current);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      if (wrapRef.current?.contains(renderer.domElement)) {
        wrapRef.current.removeChild(renderer.domElement);
      }
      const toRemove = [];
      scene.traverse((o) => {
        if (o.isMesh || o.userData?.isGrid) toRemove.push(o);
      });
      toRemove.forEach((o) => {
        o.geometry?.dispose?.();
        o.material?.dispose?.();
        o.parent?.remove(o);
      });
    };
  }, []);

  // expose snapshot
  useImperativeHandle(
    ref,
    () => ({
      getSnapshot: () => {
        try {
          const canvas = rendererRef.current?.domElement;
          return canvas?.toDataURL?.("image/png", 0.92) || null;
        } catch {
          return null;
        }
      },
    }),
    []
  );

  // load + draw G-code
  useEffect(() => {
    (async () => {
      setErr("");
      setSimplified(false);
      if (
        !objectKey ||
        !sceneRef.current ||
        !cameraRef.current ||
        !controlsRef.current
      ) {
        return;
      }
      setLoading(true);

      const scene = sceneRef.current;
      const camera = cameraRef.current;
      const controls = controlsRef.current;

      // clear model / grid เก่า
      const toRemove = [];
      scene.traverse((o) => {
        if (o.userData?.gcodeMesh || o.userData?.isGrid) toRemove.push(o);
      });
      toRemove.forEach((o) => {
        o.geometry?.dispose?.();
        o.material?.dispose?.();
        o.parent?.remove(o);
      });
      gridRef.current = null;

      // fetch G-code
      let text = "";
      try {
        text = await fetchGcodeText({
          apiBase,
          objectKey,
          token: tokenRef.current,
        });
      } catch (e) {
        setErr(
          e?.message === "too-large"
            ? "G-code too large"
            : "Failed to fetch G-code"
        );
        setLoading(false);
        return;
      }
      if (!text) {
        setErr("Empty G-code");
        setLoading(false);
        return;
      }

      // parse
      let parsed;
      try {
        parsed = parseGcode(text, { last });
      } catch (e) {
        setErr(e?.message || "Parse error");
        setLoading(false);
        return;
      }

      const { extru, bboxAll, bboxModel, clipped } = parsed;
      setSimplified(Boolean(clipped));

      // hide set: auto จาก preset ถ้าไม่ได้ส่ง hide มา
      // 🔧 แก้ให้ preset "clean" ไม่ซ่อน support อีกต่อไป
      const hideAuto =
        preset === "clean"
          ? "travel,infill,solid,bridge,gap,skirt,other"
          : "travel";
      const hideSet = new Set(
        String(hide ?? hideAuto)
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      );

      // ยก Z-bias ให้ผิวเด่น
      const FEATURE_ZBIAS = {
        perimeter: 0.012,
        external: 0.012,
        overhang: 0.01,
        top_solid: 0.008,
        solid: 0.002,
        skirt: 0.001,
      };

      // model group
      const model = new THREE.Group();
      model.userData.gcodeMesh = true;

      for (const key of DRAW_ORDER) {
        if (hideSet.has(key)) continue;
        const segs = extru[key] || [];
        if (!segs.length) continue;

        const geo = makeRibbonGeometry(
          segs,
          widthMM,
          Z_LIFT_PER_LYR,
          FEATURE_ZBIAS[key] || 0
        );
        if (!geo) continue;

        const mat = new THREE.MeshBasicMaterial({
          color: COLORS[key] || new THREE.Color(0.3, 0.8, 0.3),
          side: THREE.DoubleSide,
        });
        const mesh = new THREE.Mesh(geo, mat);
        model.add(mesh);
      }

      // center on XY & place at Z=0
      const [mx0, my0, mz0, mx1, my1, mz1] = bboxModel;
      const mcx = (mx0 + mx1) / 2;
      const mcy = (my0 + my1) / 2;
      const mcz = (mz0 + mz1) / 2;
      model.position.set(-mcx, -mcy, -mz0);
      scene.add(model);

      // grid
      const { size, divisions } = gridSpecFromBBox(bboxModel, {
        padMM: gridPadMM,
        minSize: minGridSize,
      });
      const grid = new THREE.GridHelper(size, divisions, 0xdce6e6, 0xdce6e6);
      grid.rotation.set(Math.PI / 2, 0, 0);
      grid.position.set(0, 0, 0);
      grid.userData.isGrid = true;
      scene.add(grid);
      gridRef.current = grid;

      // camera fit
      const fitBox = fitTarget === "all" ? bboxAll : bboxModel;
      const [ax0, ay0, az0, ax1, ay1, az1] = fitBox;
      const sizeX = Math.max(1e-6, ax1 - ax0);
      const sizeY = Math.max(1e-6, ay1 - ay0);
      const sizeZ = Math.max(1e-6, az1 - az0);

      const target = new THREE.Vector3(0, 0, mcz - mz0);
      controls.target.copy(target);

      const vFov = (camera.fov * Math.PI) / 180;
      const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
      const radius = 0.5 * Math.max(sizeX, sizeY, sizeZ) * fitFactor;
      const dist = Math.max(
        radius / Math.sin(vFov / 2),
        radius / Math.sin(hFov / 2)
      );

      const dir = new THREE.Vector3(-0.55, -0.75, 0.95).normalize();
      camera.position.copy(target).addScaledVector(dir, dist);
      camera.near = Math.max(0.01, dist * 0.001);
      camera.far = Math.max(camera.far, dist * 10);
      camera.updateProjectionMatrix();
      controls.update();

      setLoading(false);
    })();
  }, [
    objectKey,
    apiBase,
    last,
    widthMM,
    hide,
    preset,
    fitTarget,
    fitFactor,
    gridPadMM,
    minGridSize,
  ]); // << ไม่มี token ใน deps แล้ว

  return (
    <div
      ref={wrapRef}
      style={{
        width: "100%",
        height,
        borderRadius: 12,
        overflow: "hidden",
        background: "#f6f7f9",
        position: "relative",
        ...style,
      }}
      aria-label="G-code WebGL preview (drag to orbit, wheel to zoom)"
      role="img"
    >
      {!objectKey && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            placeItems: "center",
            color: "#9aa0a6",
          }}
        >
          No G-code provided.
        </div>
      )}
      {loading && !err && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            placeItems: "center",
            color: "#9aa0a6",
          }}
        >
          Loading G-code…
        </div>
      )}
      {simplified && !err && (
        <div
          style={{
            position: "absolute",
            left: 8,
            bottom: 8,
            background: "rgba(255,255,255,0.9)",
            padding: "4px 8px",
            borderRadius: 6,
            fontSize: 12,
            color: "#64748b",
          }}
        >
          Preview simplified (large file)
        </div>
      )}
      {err && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            placeItems: "center",
            color: "#b74d4d",
          }}
        >
          {String(err)}
        </div>
      )}
    </div>
  );
}

// ห่อด้วย forwardRef + React.memo เพื่อลด re-render ที่ไม่จำเป็น
const ForwardedGcodeWebGLPreview = forwardRef(GcodeWebGLPreviewInner);

export default React.memo(
  ForwardedGcodeWebGLPreview,
  (prev, next) =>
    prev.objectKey === next.objectKey &&
    prev.apiBase === next.apiBase &&
    prev.last === next.last &&
    prev.widthMM === next.widthMM &&
    prev.hide === next.hide &&
    prev.preset === next.preset &&
    prev.fitTarget === next.fitTarget &&
    prev.fitFactor === next.fitFactor &&
    prev.gridPadMM === next.gridPadMM &&
    prev.minGridSize === next.minGridSize &&
    prev.height === next.height
  // token / style เปลี่ยนก็ไม่ต้อง re-render preview
);
