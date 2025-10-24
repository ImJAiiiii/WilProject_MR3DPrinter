// utils/printerId.js
export function slugPrinterId(s = '') {
  return (s || '')
    .replace(/[^\w\s\-]+/g, '')  // เอาเฉพาะตัวอักษร/ตัวเลข/ขีด/ช่องว่าง
    .trim()
    .replace(/\s+/g, '-')        // ช่องว่าง -> ขีด
    .toLowerCase() || 'prusa-core-one';
}
