import fs from 'node:fs';
import path from 'node:path';
import { APP_PORT, NO_FRONTEND_PORT, harnessRoot } from './env';

/** The suite fails if anything tried to leave loopback, even if every spec passed. */
export default function globalTeardown() {
  for (const port of [APP_PORT, NO_FRONTEND_PORT]) {
    const log = path.join(harnessRoot(port), 'blocked-network.log');
    if (fs.existsSync(log)) {
      const hosts = [...new Set(fs.readFileSync(log, 'utf8').split('\n').filter(Boolean))];
      throw new Error(`The app tried to reach non-loopback hosts past the stubs: ${hosts.join(', ')}`);
    }
  }
}
