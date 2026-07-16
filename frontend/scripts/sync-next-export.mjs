import { cp, mkdir, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';

if (!existsSync('out')) {
  throw new Error('Next static export directory "out" was not created');
}

await rm('dist', { recursive: true, force: true });
await mkdir('dist', { recursive: true });
await cp('out', 'dist', { recursive: true });