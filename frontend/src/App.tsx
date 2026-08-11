import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider } from '@mui/material/styles';
import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { getSetupStatus } from './api/client';
import FirstRunSetup from './components/FirstRunSetup';
import Shell from './components/layout/Shell';
import Config from './views/Config';
import Import from './views/Import';
import Jobs from './views/Jobs';
import Library from './views/Library';
import LibraryChanges from './views/LibraryChanges';
import Playlists from './views/Playlists';
import Submissions from './views/Submissions';
import System from './views/System';
import { theme } from './theme';

// Extracted from App so tests can mount the same route tree under a
// MemoryRouter instead of BrowserRouter, without changing production
// behavior (App below still renders this under BrowserRouter as before).
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/library" replace />} />
        <Route path="library"   element={<Library />} />
        <Route path="changes"   element={<LibraryChanges />} />
        <Route path="import"    element={<Import />} />
        <Route path="clean"     element={<Navigate to="/jobs" replace />} />
        <Route path="playlists" element={<Playlists />} />
        <Route path="jobs"      element={<Jobs />} />
        <Route path="config"    element={<Config />} />
        <Route path="system"    element={<System />} />
        <Route path="setup"     element={<Navigate to="/system" replace />} />
        <Route path="submissions" element={<Submissions />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  const [firstRunRequired, setFirstRunRequired] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;
    getSetupStatus()
      .then((data) => {
        if (!active) return;
        setFirstRunRequired(data.first_run?.required === true || data.auth?.first_run_required === true);
      })
      .catch(() => {
        if (!active) return;
        setFirstRunRequired(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (firstRunRequired === true) {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <FirstRunSetup />
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </ThemeProvider>
  );
}
