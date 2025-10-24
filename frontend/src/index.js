// src/index.js
import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import './App.css';
import App from './App';
import reportWebVitals from './reportWebVitals';
import { AuthProvider } from './auth/AuthContext';

// fonts
import '@fontsource/inter/400.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/noto-sans-thai/400.css';
import '@fontsource/noto-sans-thai/600.css';
import '@fontsource/noto-sans-thai/700.css';

// 👇 ปิด StrictMode เฉพาะตอน development เพื่อกัน useEffect ทำงานซ้ำ (เช่น WebSocket ต่อ 2 ครั้ง)
const Shell = process.env.NODE_ENV === 'development' ? React.Fragment : React.StrictMode;

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <Shell>
    <AuthProvider>
      <App />
    </AuthProvider>
  </Shell>
);

// ถ้าอยากดู performance logs ให้เปิดบรรทัดล่างนี้
// reportWebVitals(console.log);
reportWebVitals();
