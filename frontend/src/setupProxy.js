// frontend/src/setupProxy.js
const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function (app) {
  const target = 'http://localhost:8000';

  app.use(
    [
      '/printers',
      '/history',
      '/api',
      '/files',
      '/auth',
      '/notifications',
      '/ws',
      '/healthz',
    ],
    createProxyMiddleware({
      target,
      changeOrigin: true,
      ws: true,                 // สำคัญ: proxy WebSocket ด้วย (ws://)
      logLevel: 'silent',
    })
  );
};
