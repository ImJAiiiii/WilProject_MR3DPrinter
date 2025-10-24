//src/api/base.js

const ABSOLUTE_URL = /^https?:\/\//i;

export const apiFetch = (path, options = {}) => {
  if (ABSOLUTE_URL.test(path)) {
    if (process.env.NODE_ENV !== 'production') {
      console.warn('[API] Use relative paths only:', path);
    } else {
      throw new Error('Do not use absolute API URLs in production.');
    }
  }
  return fetch(path, {
    // ถ้าใช้ cookie session ให้เปิดอันนี้
    // credentials: 'include',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
};
