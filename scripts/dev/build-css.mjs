import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import postcss from 'postcss';
import tailwindcss from '@tailwindcss/postcss';

const input = resolve('app/web/styles/app.css');
const output = resolve('app/web/static/app.css');
const css = await readFile(input, 'utf8');
const result = await postcss([tailwindcss()]).process(css, { from: input, to: output });
await mkdir(dirname(output), { recursive: true });
await writeFile(output, result.css);
