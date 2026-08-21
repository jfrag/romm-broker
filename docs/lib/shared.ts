export const appName = 'webstation-broker';
export const docsRoute = '/docs';
export const docsImageRoute = '/og/docs';
export const docsContentRoute = '/llms.mdx/docs';

// Where the "edit on GitHub" links point. The fork that builds the site can
// override the owner with NEXT_PUBLIC_GITHUB_USER so links land on its own
// tree while a change is in review.
export const gitConfig = {
  user: process.env.NEXT_PUBLIC_GITHUB_USER ?? 'romm-streaming',
  repo: process.env.NEXT_PUBLIC_GITHUB_REPO ?? 'romm-broker',
  branch: 'master',
};

// The base path the site is exported under ('' locally, '/romm-broker' on
// GitHub Pages). Needed wherever a URL is built by hand rather than through
// next/link, which prefixes it automatically.
export const basePath = process.env.NEXT_PUBLIC_BASE_PATH?.replace(/\/$/, '') ?? '';
