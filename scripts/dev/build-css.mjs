import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import postcss from 'postcss';
import tailwindcss from '@tailwindcss/postcss';

const input = resolve('app/web/styles/app.css');
const output = resolve('app/web/static/app.css');
const css = await readFile(input, 'utf8');
const result = await postcss([tailwindcss()]).process(css, { from: input, to: output });
await mkdir(dirname(output), { recursive: true });

const iconDirectory = resolve('app/web/static/ui-icons');
const icons = new Map();
let extractedIcons = 0;
const builtCss = result.css.replace(/url\("data:image\/svg\+xml,([^\"]+)"\)/g, (_match, encodedSvg) => {
  let svg;
  try {
    svg = decodeURIComponent(encodedSvg);
  } catch (error) {
    throw new Error('Could not decode generated Flowbite SVG data URI', { cause: error });
  }
  if (!svg.startsWith('<svg ') || !svg.endsWith('</svg>')) {
    throw new Error('Generated Flowbite SVG data URI did not contain a complete SVG');
  }
  const asset = `${svg}\n`;
  const digest = createHash('sha256').update(asset).digest('hex');
  const filename = `${digest}.svg`;
  icons.set(filename, asset);
  extractedIcons += 1;
  return `url("ui-icons/${filename}")`;
});

await mkdir(iconDirectory, { recursive: true });
for (const [filename, svg] of icons) {
  await writeFile(resolve(iconDirectory, filename), svg);
}
if (/url\(\s*["']?data:/i.test(builtCss)) {
  throw new Error('Generated CSS still contains a data: URL');
}
if (/url\(\s*["']?https?:\/\//i.test(builtCss)) {
  throw new Error('Generated CSS still contains a remote asset URL');
}
await writeFile(output, builtCss);
console.log(`Built ${output}; extracted ${extractedIcons} SVG references into ${icons.size} local icons; no data or remote asset URLs.`);
