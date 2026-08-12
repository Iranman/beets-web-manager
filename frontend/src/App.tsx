import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider } from '@mui/material/styles';
import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { getAuthMe, logout } from './api/client';
import FirstRunSetup from './components/FirstRunSetup';
import Shell from './components/layout/Shell';
import Config from './views/Config';
import Import from './views/Import';
import Jobs from './views/Jobs';
import Library from './views/Library';
import LibraryChanges from './views/LibraryChanges';
import Login from './views/Login';
import Playlists from './views/Playlists';
import Submissions from './views/Submissions';
import System from './views/System';
import { theme } from './theme';

interface AppRoutesProps {
  authenticated: boolean;
  setupComplete: boolean;
  firstRunRequired: boolean;
  username?: string;
  onLogout?: () => void;
  onLoginSuccess?: () => void;
}

export function AppRoutes({
  authenticated,
  setupComplete,
  firstRunRequired,
  username,
  onLogout,
  onLoginSuccess,
}: AppRoutesProps) {
  return (
    <Routes>
      <Route
        path="login"
        element={
          firstRunRequired ? (
            <Navigate to="/setup" replace />
          ) : authenticated ? (
            <Navigate to={setupComplete ? "/library" : "/setup"} replace />
          ) : (
            <Login onSuccess={onLoginSuccess} />
          )
        }
      />
      <Route
        path="setup"
        element={
          setupComplete ? (
            authenticated ? <Navigate to="/system" replace /> : <Navigate to="/login" replace />
          ) : firstRunRequired || authenticated ? (
            <FirstRunSetup />
          ) : (
            <Navigate to="/login" replace />
          )
        }
      />
      <Route
        element={
          !setupComplete ? (
            <Navigate to={firstRunRequired || authenticated ? "/setup" : "/login"} replace />
          ) : authenticated ? (
            <Shell username={username} onLogout={onLogout} />
          ) : (
            <Navigate to="/login" replace />
          )
        }
      >
        <Route index element={<Navigate to="/library" replace />} />
        <Route path="library" element={<Library />} />
        <Route path="changes" element={<LibraryChanges />} />
        <Route path="import" element={<Import />} />
        <Route path="clean" element={<Navigate to="/jobs" replace />} />
        <Route path="playlists" element={<Playlists />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="config" element={<Config />} />
        <Route path="system" element={<System />} />
        <Route path="submissions" element={<Submissions />} />
      </Route>
      <Route
        path="*"
        element={
          <Navigate
            to={!setupComplete ? (firstRunRequired || authenticated ? '/setup' : '/login') : authenticated ? '/library' : '/login'}
            replace
          />
        }
      />
    </Routes>
  );
}

export default function App() {
  const [setupComplete, setSetupComplete] = useState<boolean | null>(null);
  const [firstRunRequired, setFirstRunRequired] = useState<boolean | null>(null);
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [username, setUsername] = useState<string>('admin');

  const checkAuth = () => {
    getAuthMe()
      .then((data) => {
        setFirstRunRequired(data.first_run_required === true);
        setSetupComplete(data.setup_complete === true && data.first_run_required !== true);
        setAuthenticated(data.authenticated === true);
        if (data.username) setUsername(data.username);
      })
      .catch(() => {
        // Fallback if network or early error
        setFirstRunRequired(true);
        setSetupComplete(false);
        setAuthenticated(false);
      });
  };

  useEffect(() => {
    checkAuth();
  }, []);

  const handleLogout = async () => {
    try {
      await logout();
    } catch {
      // Ignore
    }
    setAuthenticated(false);
  };

  if (setupComplete === null || firstRunRequired === null || authenticated === null) {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <div className="flex min-h-screen items-center justify-center bg-graphite-950 text-zinc-400">
          <div className="text-sm font-semibold animate-pulse">Loading Beets Web Manager...</div>
        </div>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <BrowserRouter>
        <AppRoutes
          authenticated={authenticated}
          setupComplete={setupComplete}
          firstRunRequired={firstRunRequired}
          username={username}
          onLogout={handleLogout}
          onLoginSuccess={checkAuth}
        />
      </BrowserRouter>
    </ThemeProvider>
  );
}
