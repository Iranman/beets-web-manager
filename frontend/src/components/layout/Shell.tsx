import { Menu, MenuButton, MenuItem, MenuItems } from '@headlessui/react';
import { useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router';
import { getHealth, logout, restartApp } from '../../api/client';
import { useEngineProbe, useGlobalJobs } from '../../lib/hooks';
import { EngineOfflineBanner, EngineOfflineContext } from '../EngineOfflineBanner';
import { RecordMark } from '../RecordMark';

interface Tab {
  to: string;
  label: string;
}

interface ShellProps {
  username?: string;
  onLogout?: () => void;
}

const TABS: Tab[] = [
  { to: '/library',   label: 'Library' },
  { to: '/import',    label: 'Workbench' },
  { to: '/playlists', label: 'Playlists' },
  { to: '/jobs',      label: 'Jobs & Cleanup' },
];

// Pages reached from the System menu; the menu button reads as current on them.
const SYSTEM_MENU_PATHS = ['/config', '/system'];

const BADGE = 'ml-1.5 rounded-full px-1.5 py-0.5 text-xs font-semibold leading-none tabular-nums';

// Colour is never the only signal (DESIGN.md, The Label-With-Colour Rule): each pill carries its word
// for assistive tech and as a tooltip, and the header status line spells both counts out from `sm` up.
// Failures stay visible while other jobs run.
function JobsBadge({ running, failed }: { running: number; failed: number }) {
  return (
    <>
      {running > 0 && (
        <span className={`${BADGE} bg-info text-graphite-950`} title={`${running} running`}>
          <span aria-hidden="true">{running}</span>
          <span className="sr-only"> ({running} running)</span>
        </span>
      )}
      {failed > 0 && (
        <span className={`${BADGE} bg-danger text-zinc-50`} title={`${failed} failed`}>
          <span aria-hidden="true">{failed}</span>
          <span className="sr-only"> ({failed} failed)</span>
        </span>
      )}
    </>
  );
}

export default function Shell({ username, onLogout }: ShellProps) {
  const { running, failed } = useGlobalJobs();
  const engineOffline = useEngineProbe();
  const [restarting, setRestarting] = useState(false);
  const [restartMsg, setRestartMsg] = useState('');
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const onSystemPage = SYSTEM_MENU_PATHS.some((p) => pathname.startsWith(p));

  const handleRestart = async () => {
    if (!window.confirm('Restart the app? The UI will be unavailable for a few seconds.')) return;
    setRestarting(true);
    setRestartMsg('Restarting…');
    try {
      await restartApp();
    } catch {
      // Expected: the server drops the connection during restart.
    }
    let back = false;
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      try {
        await getHealth();
        back = true;
        break;
      } catch {
        // Still restarting
      }
    }
    setRestarting(false);
    setRestartMsg(back ? 'Back online' : 'Restarted');
    setTimeout(() => setRestartMsg(''), 4000);
  };

  const handleLogoutClick = async () => {
    if (onLogout) {
      onLogout();
    } else {
      try {
        await logout();
      } catch {
        // Ignore
      }
      window.location.href = '/login';
    }
  };

  return (
    <div className="flex min-h-screen min-w-0 flex-col bg-graphite-950 text-zinc-100">
      {/* Flat walnut plank: a shelf-edge border, no shadow or blur (DESIGN.md, The Flat Shelf Rule). */}
      <header className="sticky top-0 z-20 border-b border-graphite-700 bg-graphite-900">
        <div className="mx-auto flex h-[var(--bw-header-h)] max-w-full items-center gap-x-1 px-3 sm:px-4 lg:max-w-screen-2xl">
          {/* The wordmark sits in its own bay, closed by a divider like a record-bin card. */}
          <span className="mr-1 flex h-full shrink-0 select-none items-center gap-2.5 sm:mr-3 sm:border-r sm:border-graphite-800 sm:pr-4">
            <RecordMark playing={running > 0} />
            <span className="hidden whitespace-nowrap font-display text-base font-semibold text-brass-200 sm:inline">
              Beets<span className="hidden lg:inline"> Web Manager</span>
            </span>
          </span>

          {/* Tabs scroll sideways on narrow screens instead of wrapping (#607). */}
          <nav aria-label="Primary" data-testid="primary-nav" className="flex h-full min-w-0 flex-1 overflow-x-auto">
            {TABS.map(({ to, label }) => {
              const isJobs = to === '/jobs';
              return (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    [
                      'flex shrink-0 items-center whitespace-nowrap border-b-2 px-2 text-sm font-medium transition-colors duration-150 focus-visible:-outline-offset-2! sm:px-3',
                      isActive
                        ? 'border-brass-400 bg-brass-950/60 text-zinc-50'
                        : 'border-transparent text-zinc-300 hover:border-brass-700 hover:bg-brass-950/40 hover:text-zinc-50 active:bg-brass-950/70',
                    ].join(' ')
                  }
                >
                  {label}
                  {isJobs && <JobsBadge running={running} failed={failed} />}
                </NavLink>
              );
            })}
          </nav>

          <div className="ml-auto flex shrink-0 items-center gap-2 sm:gap-3">
            {restartMsg && (
              <span role="status" className="text-xs text-success">{restartMsg}</span>
            )}
            {!restartMsg && running > 0 && (
              <span className="whitespace-nowrap text-xs tabular-nums text-info">{running} running</span>
            )}
            {!restartMsg && failed > 0 && (
              <span className="whitespace-nowrap text-xs tabular-nums text-danger-text">{failed} failed</span>
            )}
            <Menu>
              <MenuButton
                disabled={restarting}
                className={`flex h-[var(--bw-header-h)] min-w-11 items-center justify-center gap-2 border-b-2 px-2.5 text-sm font-medium text-brass-300 transition-colors duration-150 hover:bg-brass-950/60 hover:text-brass-200 active:bg-brass-950/80 disabled:opacity-40 focus-visible:-outline-offset-2! data-[open]:bg-brass-950/60 data-[open]:text-brass-200 ${onSystemPage ? 'border-brass-400 bg-brass-950/60 text-brass-200' : 'border-transparent'}`}
                title="System"
              >
                <svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="3" />
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </svg>
                <span className="hidden md:inline">System</span>
              </MenuButton>
              <MenuItems
                anchor="bottom end"
                className="z-50 mt-1 min-w-[12rem] rounded-md border border-graphite-700 bg-graphite-850 py-1 shadow-lg shadow-black/40 focus-visible:border-brass-400 focus-visible:outline-none!"
              >
                <div className="border-b border-graphite-700 px-3 py-2 text-xs text-muted">
                  Signed in as <span className="font-semibold text-zinc-50">{username || 'admin'}</span>
                </div>
                <MenuItem>
                  <button
                    className="flex min-h-9 w-full items-center px-3 text-sm text-zinc-300 hover:bg-graphite-800 hover:text-zinc-50 active:bg-graphite-700 data-[focus]:bg-graphite-800 data-[focus]:text-zinc-50 data-[focus]:outline-2 data-[focus]:-outline-offset-2 data-[focus]:outline-brass-300"
                    onClick={() => void navigate('/config')}
                  >
                    Settings
                  </button>
                </MenuItem>
                <MenuItem>
                  <button
                    className="flex min-h-9 w-full items-center px-3 text-sm text-zinc-300 hover:bg-graphite-800 hover:text-zinc-50 active:bg-graphite-700 data-[focus]:bg-graphite-800 data-[focus]:text-zinc-50 data-[focus]:outline-2 data-[focus]:-outline-offset-2 data-[focus]:outline-brass-300"
                    onClick={() => void navigate('/system')}
                  >
                    System
                  </button>
                </MenuItem>
                {/* App-level actions sit apart from the pages above. */}
                <MenuItem>
                  <button
                    className="flex min-h-9 w-full items-center border-t border-graphite-700 px-3 text-sm text-danger-text hover:bg-danger/25 hover:text-zinc-50 active:bg-danger/40 data-[focus]:bg-danger/25 data-[focus]:text-zinc-50 data-[focus]:outline-2 data-[focus]:-outline-offset-2 data-[focus]:outline-brass-300 disabled:opacity-50"
                    disabled={restarting}
                    onClick={() => void handleRestart()}
                  >
                    {restarting ? 'Restarting…' : 'Restart app'}
                  </button>
                </MenuItem>
                <MenuItem>
                  <button
                    className="flex min-h-9 w-full items-center px-3 text-sm text-zinc-300 hover:bg-graphite-800 hover:text-zinc-50 active:bg-graphite-700 data-[focus]:bg-graphite-800 data-[focus]:text-zinc-50 data-[focus]:outline-2 data-[focus]:-outline-offset-2 data-[focus]:outline-brass-300"
                    onClick={() => void handleLogoutClick()}
                  >
                    Sign Out
                  </button>
                </MenuItem>
              </MenuItems>
            </Menu>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full min-w-0 max-w-screen-2xl flex-1 px-3 py-4 sm:px-4 sm:py-6">
        <EngineOfflineContext.Provider value={engineOffline}>
          <EngineOfflineBanner />
          <Outlet />
        </EngineOfflineContext.Provider>
      </main>
    </div>
  );
}
