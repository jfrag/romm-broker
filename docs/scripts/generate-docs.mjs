// Turns the JSON that `fumapy-generate webstation_broker` writes into the MDX
// pages under content/docs/developer/reference.
//
// Run from the repository root:
//
//   PYTHONPATH=. fumapy-generate webstation_broker --dir docs
//   cd docs && npm run generate
//
// The output directory is gitignored; the docs workflow regenerates it on
// every build so the reference always matches the committed source.

import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { rimraf } from 'rimraf';
import * as Python from 'fumadocs-python';

const here = path.dirname(fileURLToPath(import.meta.url));
const jsonPath = path.join(here, '..', 'webstation_broker.json');
const outDir = path.join(here, '..', 'content', 'docs', 'developer', 'reference');
const baseUrl = '/docs/developer/reference';
const packageName = 'webstation_broker';

// fumadocs-python only renders the `text` and `admonition` docstring sections
// and silently drops the rest, so the Google-style sections it does not know
// are rewritten into markdown text before conversion.
function sectionToText(section) {
  const items = Array.isArray(section.value) ? section.value : [];
  switch (section.kind) {
    case 'raises':
      return [
        '**Raises**',
        ...items.map((item) => `- \`${item.annotation ?? 'Exception'}\`: ${item.description ?? ''}`),
      ].join('\n');
    case 'warns':
      return ['**Warns**', ...items.map((item) => `- \`${item.annotation ?? ''}\`: ${item.description ?? ''}`)].join(
        '\n',
      );
    case 'yields':
    case 'receives':
      return [
        `**${section.kind === 'yields' ? 'Yields' : 'Receives'}**`,
        ...items.map((item) => {
          const label = [item.name, item.annotation && `\`${item.annotation}\``].filter(Boolean).join(' ');
          return `- ${label ? `${label}: ` : ''}${item.description ?? ''}`;
        }),
      ].join('\n');
    case 'examples':
      return [
        '**Examples**',
        ...items.map(([kind, text]) => (kind === 'examples' ? `\`\`\`python\n${text}\n\`\`\`` : text)),
      ].join('\n\n');
    case 'deprecated':
      return `**Deprecated** since ${section.value?.version ?? ''}: ${section.value?.description ?? ''}`;
    default:
      return null;
  }
}

function rewriteDocstring(sections) {
  if (!Array.isArray(sections)) return sections;
  return sections.map((section) => {
    if (section.kind === 'text' || section.kind === 'admonition') return section;
    const text = sectionToText(section);
    return text === null ? section : { kind: 'text', value: text };
  });
}

function rewriteFunction(func) {
  func.docstring = rewriteDocstring(func.docstring);
}

function rewriteClass(cls) {
  cls.docstring = rewriteDocstring(cls.docstring);
  for (const func of Object.values(cls.functions ?? {})) rewriteFunction(func);
}

function rewriteModule(mod) {
  mod.docstring = rewriteDocstring(mod.docstring);
  for (const func of Object.values(mod.functions ?? {})) rewriteFunction(func);
  for (const cls of Object.values(mod.classes ?? {})) rewriteClass(cls);
  for (const sub of Object.values(mod.modules ?? {})) rewriteModule(sub);
}

// The converter escapes `<`, `{` and `}` everywhere in prose, inline code
// included, and CommonMark shows a backslash inside a code span literally.
// Undo it inside inline code, leaving fenced blocks (which are never escaped)
// alone.
function unescapeInlineCode(markdown) {
  const parts = markdown.split(/(```[\s\S]*?```)/g);
  return parts
    .map((part, index) => {
      if (index % 2 === 1) return part;
      return part.replace(/`([^`\n]*)`/g, (_match, code) =>
        `\`${code.replaceAll('\\<', '<').replaceAll('\\{', '{').replaceAll('\\}', '}')}\``,
      );
    })
    .join('');
}

function frontmatter(obj) {
  const lines = Object.entries(obj).map(([key, value]) => `${key}: ${JSON.stringify(value)}`);
  return `---\n${lines.join('\n')}\n---`;
}

function firstSentence(text) {
  if (!text) return undefined;
  const line = text.split('\n')[0].trim();
  return line.length > 0 ? line : undefined;
}

async function generate() {
  let raw;
  try {
    raw = await fs.readFile(jsonPath, 'utf8');
  } catch {
    console.error(`missing ${jsonPath}`);
    console.error('generate it from the repository root with:');
    console.error(`  PYTHONPATH=. fumapy-generate ${packageName} --dir docs`);
    process.exit(1);
  }

  const pkg = JSON.parse(raw);
  rewriteModule(pkg);

  // Index descriptions by dotted path so each page can carry a one-line
  // description in its frontmatter (the sidebar and search show it).
  const descriptions = new Map();
  const collect = (mod) => {
    descriptions.set(mod.path, firstSentence(mod.description));
    for (const cls of Object.values(mod.classes ?? {})) descriptions.set(cls.path, firstSentence(cls.description));
    for (const sub of Object.values(mod.modules ?? {})) collect(sub);
  };
  collect(pkg);

  const files = Python.convert(pkg, { baseUrl });

  await rimraf(outDir);
  for (const file of files) {
    // The converter prefixes every path with the package name and every link
    // with `${baseUrl}/${packageName}`; strip both so module paths map onto
    // the reference folder directly (`emulators/base` rather than
    // `webstation_broker/emulators/base`).
    const segments = file.path.split('/');
    const relative = segments[0] === packageName ? segments.slice(1).join('/') : file.path;
    const target = path.join(outDir, relative);

    const dotted = file.path.replace(/\/index\.mdx$/, '').replace(/\.mdx$/, '').replaceAll('/', '.');
    const isPackageRoot = dotted === packageName;
    const title = isPackageRoot ? packageName : dotted.replace(`${packageName}.`, '');
    const description = descriptions.get(dotted);

    const fm = { title, ...(description ? { description } : {}) };
    const content = unescapeInlineCode(file.content.replaceAll(`${baseUrl}/${packageName}/`, `${baseUrl}/`));

    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, `${frontmatter(fm)}\n\n${content}\n`);
  }

  // Keep the package index at the top of the folder in the sidebar.
  await fs.writeFile(
    path.join(outDir, 'meta.json'),
    JSON.stringify({ title: 'Python reference', pages: ['index', '...'] }, null, 2) + '\n',
  );

  console.log(`wrote ${files.length} pages to ${path.relative(process.cwd(), outDir)}`);
}

await generate();
