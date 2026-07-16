import { Menu, MenuButton, MenuItem, MenuItems } from '@headlessui/react';
import { useState } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { getHealth, restartApp } from '../../api/client';
import { useGlobalJobs } from '../../lib/hooks';

interface Tab {
  to: string;
  label: string;
}

const TABS: Tab[] = [
  { to: '/library',   label: 'Library' },
  { to: '/import',    label: 'Import' },
  { to: '/playlists', label: 'Playlists' },
  { to: '/jobs',      label: 'Jobs' },
];

function BeetsLogo() {
  return (
    <span className="flex size-8 items-center justify-center rounded-md bg-red-700 text-white shadow-sm shadow-red-950/40 ring-1 ring-red-400/35" aria-hidden="true">
      <svg className="size-5" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 7.4c3.5 0 6.1 2.9 5.5 6.3-.5 3.4-2.8 6.2-5.5 6.2s-5-2.8-5.5-6.2c-.6-3.4 2-6.3 5.5-6.3Z" fill="currentColor" />
        <path d="M12 7.9c-.2-2.5.8-4.1 3-4.8.3 2.3-.8 3.9-3 4.8Z" fill="#fca5a5" />
        <path d="M11.8 7.9c-2.4-.2-3.9-1.3-4.6-3.2 2.3-.1 3.9.9 4.6 3.2Z" fill="#fecaca" />
        <path d="M12 7.7V4.4" stroke="#fee2e2" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </span>
  );
}

function JobsBadge({ running, failed }: { running: number; failed: number }) {
  if (running > 0) {
    return (
      <span className="ml-1.5 rounded-full bg-amber-500/90 px-1.5 py-px text-[0.62rem] font-bold leading-none text-zinc-950">
        {running}
      </span>
    );
  }
  if (failed > 0) {
    return (
      <span className="ml-1.5 rounded-full bg-red-700 px-1.5 py-px text-[0.62rem] font-bold leading-none text-white">
        {failed}
      </span>
    );
  }
  return null;
}

export default function Shell() {
  const { running, failed } = useGlobalJobs();
  const [restarting, setRestarting] = useState(false);
  const [restartMsg, setRestartMsg] = useState('');
  const navigate = useNavigate();

  const handleRestart = async () => {
    if (!window.confirm('Restart the app? The UI will be unavailable for a few seconds.')) return;
    setRestarting(true);
    setRestartMsg('Restarting...');
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

  return (
    <div className="flex min-h-screen min-w-0 flex-col bg-graphite-950 text-zinc-100">
      <header className="sticky top-0 z-20 border-b border-red-900/55 bg-graphite-900/96 shadow-sm shadow-red-950/20 backdrop-blur">
        <div className="relative min-w-0 before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-red-600 before:via-red-500 before:to-transparent">
          <div className="mx-auto flex max-w-full flex-wrap items-center gap-x-1 gap-y-0 px-3 sm:flex-nowrap sm:overflow-x-auto sm:px-4 lg:max-w-screen-2xl">
            <span className="mr-3 flex shrink-0 select-none items-center gap-2 py-2.5 sm:mr-5">
              <BeetsLogo />
              <span className="text-sm font-black uppercase tracking-widest text-red-300">
                Beets
              </span>
            </span>

            {TABS.map(({ to, label }) => {
              const isJobs = to === '/jobs';
              return (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    [
                      'flex shrink-0 items-center border-b-2 px-2.5 py-3 text-[0.82rem] font-medium transition-colors sm:px-3',
                      isActive
                        ? 'border-red-500 bg-red-950/35 text-white shadow-[inset_0_-1px_0_rgba(239,68,68,0.35)]'
                        : 'border-transparent text-zinc-300 hover:border-red-700/70 hover:bg-red-950/18 hover:text-white',
                    ].join(' ')
                  }
                >
                  {label}
                  {isJobs && <JobsBadge running={running} failed={failed} />}
                </NavLink>
              );
            })}

            <div className="ml-auto flex shrink-0 items-center gap-3">
              {restartMsg && (
                <span className="text-[0.72rem] text-emerald-400">{restartMsg}</span>
              )}
              {!restartMsg && running > 0 && (
                <span className="flex items-center gap-1.5 text-[0.72rem] text-amber-300">
                  <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-amber-300" />
                  {running} running
                </span>
              )}
              <Menu>
                <MenuButton
                  disabled={restarting}
                  className="flex items-center rounded-md px-1.5 py-1 text-red-300 transition-colors hover:bg-red-950/35 hover:text-white disabled:opacity-40"
                  title="Settings"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                  </svg>
                </MenuButton>
                <MenuItems
                  anchor="bottom end"
                  className="z-50 mt-1 min-w-[10rem] rounded-md border border-red-900/50 bg-graphite-850 py-1 shadow-xl shadow-red-950/25"
                >
                  <MenuItem>
                    <button
                      className="flex w-full items-center gap-2 px-3 py-2 text-sm text-zinc-300 hover:bg-graphite-800 hover:text-white"
                      onClick={() => void navigate('/config')}
                    >
                      Settings
                    </button>
                  </MenuItem>
                  <MenuItem>
                    <button
                      className="flex w-full items-center gap-2 px-3 py-2 text-sm text-red-400 hover:bg-red-950/30 hover:text-red-200 disabled:opacity-50"
                      disabled={restarting}
                      onClick={() => void handleRestart()}
                    >
                      {restarting ? 'Restarting...' : 'Restart app'}
                    </button>
                  </MenuItem>
                </MenuItems>
              </Menu>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full min-w-0 max-w-screen-2xl flex-1 px-3 py-4 sm:px-4 sm:py-5">
        <Outlet />
      </main>
    </div>
  );
}
