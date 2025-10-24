// src/api/useApi.js
import { useMemo } from "react";
import { makeApi } from "./index";
import { useAuth } from "../auth/AuthContext";

/**
 * ใช้ในคอมโพเนนต์ฝั่ง FE เพื่อเรียก API ของ backend
 * เพิ่มเมธอดที่จำเป็นต่อ flow:
 *  - slicer.sliceFromStorage({ objectKey, jobName, slicing })
 *  - slicer.inspectGcode({ objectKey })
 *  - print.queue(printerId, payload)
 *
 * หมายเหตุ:
 * - ไม่เปลี่ยนพฤติกรรมเดิมของ makeApi(); แค่ "ขยาย" ความสามารถ
 * - ถ้า base api มี http.get/post หรือ get/post/request จะพยายามใช้แบบที่มีอยู่
 */
export function useApi() {
  const { token, logout } = useAuth() || {};

  return useMemo(() => {
    const base = makeApi({
      token,
      onUnauthorized: () => logout?.({ silent: true }),
    });

    // ---- หา helper เรียก HTTP จาก base ที่มีอยู่ (พยายามรองรับหลายสไตล์) ----
    const http = base?.http || {};
    const hasGetPost = typeof base?.get === "function" && typeof base?.post === "function";
    const hasHttpGetPost = typeof http?.get === "function" && typeof http?.post === "function";

    const doGet = async (url, opts) => {
      if (hasGetPost) return base.get(url, opts);
      if (hasHttpGetPost) return http.get(url, opts);
      if (typeof base?.request === "function") return base.request({ method: "GET", url, ...(opts || {}) });
      throw new Error("API client missing GET method");
    };

    const doPost = async (url, data, opts) => {
      if (hasGetPost) return base.post(url, data, opts);
      if (hasHttpGetPost) return http.post(url, data, opts);
      if (typeof base?.request === "function")
        return base.request({ method: "POST", url, data, ...(opts || {}) });
      throw new Error("API client missing POST method");
    };

    // ---- ส่วนขยายที่ต้องใช้กับฟลโล STL->slice->queue & G-code queue ----
    const extended = {
      // ========== SLICER ==========
      slicer: {
        /**
         * ให้ BE สไลซ์จากไฟล์ที่ "ยังอยู่ใน staging/*" หรือไฟล์ใน storage/*
         * originExt จะกำหนดเป็น "stl" เสมอในฟลโลนี้
         * Body ที่ BE คาดหวัง:
         *  { fileId, originExt:"stl", jobName, slicing? }
         * Response ที่คาดหวัง:
         *  { gcode_key, result:{ time_min, filament_g }, preview_image_url? }
         */
        sliceFromStorage: async ({ objectKey, jobName, slicing }) => {
          const body = {
            fileId: objectKey, // เช่น "staging/2025/09/xxx.stl"
            originExt: "stl",
            jobName: jobName || "Job",
            slicing: slicing || {},
          };
          return doPost("/api/slicer/preview", body);
        },

        /**
         * อ่าน meta เวลา/กรัม จากหัวไฟล์ G-code ใน storage
         * Query: /api/gcode/meta?object_key=...
         * Response: { time_min, time_text?, filament_g? }
         */
        inspectGcode: async ({ objectKey }) => {
          return doGet("/api/gcode/meta", { params: { object_key: objectKey } });
        },
      },

      // ========== PRINT ==========
      print: {
        /**
         * เข้าคิวพิมพ์ (ให้ BE บันทึก History + Storage และส่งไป OctoPrint เมื่อถึงคิว)
         * payload ตัวอย่าง:
         *  {
         *    name: "CameraMount_V1",
         *    time_min: 73,           // optional
         *    source: "slice"|"upload"|"storage",
         *    thumb: "data:image/png;base64,...", // optional
         *    gcode_key: "storage/2025/09/xxx.gcode"
         *  }
         */
        queue: async (printerId, payload) => {
          const q = encodeURIComponent(printerId);
          return doPost(`/api/print?printer_id=${q}`, payload);
        },
      },
    };

    // รวมของเดิม + ส่วนขยาย (ถ้าชนชื่อ ให้ส่วนขยายอยู่บนสุด)
    return {
      ...base,
      ...extended,
      slicer: { ...(base?.slicer || {}), ...(extended.slicer || {}) },
      print: { ...(base?.print || {}), ...(extended.print || {}) },
    };
  }, [token, logout]);
}
