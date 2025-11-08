// src/setupProxy.js
const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function (app) {
  // เปลี่ยน backend ได้จาก env ถ้าต้องการ: BACKEND_TARGET=http://localhost:8000
  const target = process.env.BACKEND_TARGET || 'http://127.0.0.1:8000';

  // ค่ากลางที่แข็งแรง ใช้กับทุกเส้นทาง HTTP ปกติ
  const common = {
    target,
    changeOrigin: true,   // ให้ Origin ไปเป็นของ target เพื่อตัด CORS
    xfwd: true,           // ใส่ X-Forwarded-* ให้ backend ทราบต้นทางจริง
    secure: false,        // เผื่อใช้ self-signed (ถ้า target เป็น https)
    logLevel: 'silent',   // เปลี่ยนเป็น 'debug' ถ้าต้องการดู log proxy
    timeout: 300000,      // กัน request ชิ้นใหญ่/อัปโหลดไม่ timeout ง่าย ๆ
    proxyTimeout: 300000,
    onError(err, req, res) {
      // แปลง error ให้ dev อ่านง่าย (เช่น ECONNREFUSED)
      const code = err && err.code ? ` (${err.code})` : '';
      console.error('[proxy] error' + code, err && err.message ? `- ${err.message}` : '');
      if (!res.headersSent) {
        res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
      }
      res.end('Proxy failed: backend is unreachable' + code);
    },
    // เปิดไว้เผื่อบาง backend ต้องการ header เพิ่ม
    onProxyReq(proxyReq, req, res) {
      // ตัวอย่าง: บังคับ no-cache ระหว่าง dev
      proxyReq.setHeader('Cache-Control', 'no-cache');
    },
  };

  // ----- HTTP routes (REST/ไฟล์) -----
  app.use(
    ['/api', '/files', '/storage'],
    createProxyMiddleware(common)
  );

  // ----- WebSocket (เช่น /ws สำหรับสถานะเครื่อง/แจ้งเตือน) -----
  // http-proxy-middleware จะ switch เป็น ws ให้เองเมื่อมี Upgrade header
  app.use(
    '/ws',
    createProxyMiddleware({
      ...common,
      ws: true,           // สำคัญมากสำหรับ WS
      // ปกติ backend ก็รับที่ /ws อยู่แล้ว จึงไม่ต้อง pathRewrite
      // ถ้า backend ของคุณรับที่ root ให้ใช้: pathRewrite: { '^/ws': '' }
    })
  );
};
