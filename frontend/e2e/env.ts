import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const APP_PORT = 18337;
export const NO_FRONTEND_PORT = 18338;
export const PYTHON = process.env.E2E_PYTHON || 'python';
export const PASSWORD = 'e2e-synthetic-password-not-a-secret'; // same as harness/serve.py

// A new private directory per run. The config loads this first; servers and workers inherit the variable.
process.env.BWM_E2E_RUN_DIR ||= fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'bwm-e2e-'));

/** serve.py's isolated root for a port (BWM_E2E_RUN_DIR/<port>). */
export const harnessRoot = (port = APP_PORT) => path.join(process.env.BWM_E2E_RUN_DIR as string, String(port));
