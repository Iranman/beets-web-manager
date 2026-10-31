import os from 'node:os';
import path from 'node:path';

export const APP_PORT = 18337;
export const NO_FRONTEND_PORT = 18338;
export const PYTHON = process.env.E2E_PYTHON || 'python';
export const PASSWORD = 'e2e-synthetic-password-not-a-secret'; // same as harness/serve.py

/** serve.py's isolated root for a port (tempfile.gettempdir()/bwm-e2e-<port>). */
export const harnessRoot = (port = APP_PORT) => path.join(os.tmpdir(), `bwm-e2e-${port}`);
