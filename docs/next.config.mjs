import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

// GitHub Pages serves a project site under /<repo>/, so the workflow sets
// NEXT_PUBLIC_BASE_PATH to that prefix. Local builds leave it unset and serve
// from the root.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH?.replace(/\/$/, '') || undefined;

/** @type {import('next').NextConfig} */
const config = {
  output: 'export',
  reactStrictMode: true,
  basePath,
  // Export every page as <route>/index.html. GitHub Pages answers a request
  // for /docs with a redirect to /docs/ when a docs/ directory exists, and
  // without an index file in it that is a 404; directory indexes side-step it.
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
};

export default withMDX(config);
