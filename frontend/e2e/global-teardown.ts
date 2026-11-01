import fs from 'node:fs';
import path from 'node:path';
import { APP_PORT, NO_FRONTEND_PORT, harnessRoot } from './env';

/** The suite fails if the app tried to leave loopback or start an unknown process, even if every spec passed. */
export default function globalTeardown() {
  for (const port of [APP_PORT, NO_FRONTEND_PORT]) {
    const log = path.join(harnessRoot(port), 'blocked-network.log');
    if (fs.existsSync(log)) {
      const attempts = [...new Set(fs.readFileSync(log, 'utf8').split('\n').filter(Boolean))];
      throw new Error(`The harness refused: ${attempts.join(', ')}`);
    }
  }
}
