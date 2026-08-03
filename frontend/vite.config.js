import { defineConfig } from 'vite';

// SUBFOLDER also drives the nginx templating and the FastAPI mount.
const base = (process.env.SUBFOLDER || '/streaming/').replace(/\/*$/, '/');
const prefix = base.replace(/\/$/, '');

export default defineConfig({
  base,
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // Only used when hitting the vite port directly; in the container nginx
    // routes api/ and ws/ to the broker itself.
    proxy: {
      [`${prefix}/api`]: { target: 'http://127.0.0.1:8000' },
      [`${prefix}/ws`]: { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
