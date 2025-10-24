// src/App.test.js
import { render, screen } from '@testing-library/react';
import { AuthProvider } from './auth/AuthContext';
import App from './App';

test('renders Login when no user', () => {
  render(<AuthProvider><App /></AuthProvider>);
  expect(screen.getByText(/EN\d{6,7}/i)).toBeInTheDocument(); // มีฟอร์ม ENxxxxxx
});
