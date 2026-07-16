# Beets Web React Frontend

This is the production React frontend for Beets Web Control. Flask serves the built static export at `http://192.168.0.250:8337`; Next.js is used for local development and production builds.

## Commands

```powershell
npm install
npm run dev
npm run typecheck
npm run build
npm run preview
```

During local development, Next runs at `http://localhost:3000` and rewrites `/api/*` to `http://192.168.0.250:8337`.

## Stack

- Next.js static export + React + TypeScript
- Tailwind CSS through `@tailwindcss/postcss`
- MUI / Material UI with Emotion
- Headless UI for React

## Live Routes

- `/library`: library browser, attention filter, cached art, genre coverage and genre tagging jobs.
- `/import`: AI Batch Intake, Import Review, Lidarr Wanted status/actions, and History.
- `/clean`: duplicates, album-track integrity, artist-folder merge, and no-audio folder cleanup.
- `/playlists`: playlist URL/text matching, M3U/Plex save, confirmed missing-track import/sync.
- `/jobs`: global job monitor with filters, logs, retry, kill, and clear actions.
- `/config`: integration status, library health, MusicBrainz coverage, config.yaml editor, restart.

## Deploy

```powershell
cd C:\Users\irand\beets-art-fix\frontend
npm run typecheck
npm run build

$root = '\\TRUENAS\Apps\Arrs\beets'
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item "$root\static\index.html" "$root\static\index.html.bak-$ts-react"
Copy-Item dist\* "$root\frontend\dist\" -Recurse -Force
```

`next.config.mjs` uses `output: 'export'` for production builds and a dev-only `/api/*` rewrite. `npm run build` writes Next's raw export to `out/` and syncs it to `dist/` for the existing Flask deployment path. Flask serves `/_next/static/...` chunks from `frontend/dist/_next/static/` and returns the React entrypoint for direct browser loads of app routes like `/library` and `/config`.