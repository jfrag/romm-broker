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
    // Bound to loopback and only ever reached through the container's nginx,
    // so the host check just rejects whatever hostname the container is
    // proxied under.
    allowedHosts: true,
    // Only used when hitting the vite port directly; in the container nginx
    // routes api/ and ws/ to the broker itself.
    proxy: {
      [`${prefix}/api`]: { target: 'http://127.0.0.1:8000' },
      [`${prefix}/ws`]: { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
