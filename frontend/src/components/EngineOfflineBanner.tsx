import Alert from '@mui/material/Alert';
import { createContext, useContext, useEffect, useRef } from 'react';
import { Link, useLocation } from 'react-router';
import { ENGINE_OFFLINE_MESSAGE } from '../lib/api';

/** True once the Shell's engine probe has seen the backend report the Beets
 * engine as down. Defaults to false (unknown), so nothing is disabled on a guess. */
export const EngineOfflineContext = createContext(false);

export function useEngineOffline(): boolean {
  return useContext(EngineOfflineContext);
}

/** Calls `reload` when the engine probe goes from offline to online (#618).
 * Pages still load on mount themselves, whatever the probe says. */
export function useReloadOnEngineReturn(reload: () => void): void {
  const offline = useEngineOffline();
  const wasOffline = useRef(offline);
  useEffect(() => {
    if (wasOffline.current && !offline) reload();
    wasOffline.current = offline;
  }, [offline, reload]);
}

/** The one page-level notice for an engine outage. Panels drop their own copy
 * of the same message (lib/errors panelError). */
export function EngineOfflineBanner() {
  const offline = useEngineOffline();
  const onSystem = useLocation().pathname.startsWith('/system');
  if (!offline) return null;
  return (
    <Alert severity="error" sx={{ mb: 2 }}>
      {ENGINE_OFFLINE_MESSAGE} Library data on this page can&apos;t be loaded, and library actions are disabled until it is back.{' '}
      {onSystem ? 'Check the Beets status below.' : <>Check the Beets status on the <Link className="underline" to="/system">System page</Link>.</>}
    </Alert>
  );
}
