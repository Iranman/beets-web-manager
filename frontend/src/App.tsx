import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider } from '@mui/material/styles';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import Shell from './components/layout/Shell';
import Config from './views/Config';
import Import from './views/Import';
import Jobs from './views/Jobs';
import Library from './views/Library';
import Playlists from './views/Playlists';
import { theme } from './theme';

export default function App() {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <BrowserRouter>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Navigate to="/library" replace />} />
            <Route path="library"   element={<Library />} />
            <Route path="import"    element={<Import />} />
            <Route path="clean"     element={<Navigate to="/jobs" replace />} />
            <Route path="playlists" element={<Playlists />} />
            <Route path="jobs"      element={<Jobs />} />
            <Route path="config"    element={<Config />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
}
