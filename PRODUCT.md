# Product

<!-- impeccable:product-schema 1 -->

<!-- Written by `impeccable init` from README.md, AGENTS.md and docs/ without a live interview
     (the run had no way to ask the owner). Facts below come from those files; anything
     marked "Open" was not settled there. -->

## Platform

web

## Users

Two groups, both named in AGENTS.md:

- **Experienced self-hosters who already run Beets** and want a web UI on top of it. They know Beets' concepts (imports, `config.yaml`, plugins, MusicBrainz IDs) and expect the app not to limit or override how they configured Beets.
- **\*arr-stack users (Lidarr, Plex, slskd) who are new to Beets.** They arrive from a download-automation setup and need clear onboarding, sensible defaults, and error messages that say what to do next.

Both are the administrator of their own server: one operator, signed in as the account created by the first-run setup wizard.

## Product Purpose

Beets Web Manager is a self-hosted web application for managing a Beets music library from one operator-focused interface: import review, playlist repair, acquisition queues, cleanup jobs, metadata verification and media-server synchronization. It exists because the Beets web plugin did not cover enough on its own (README.md).

Success means the operator can bring new music into the library, see what is wrong with the library, and fix it, without dropping to the `beet` CLI, and without the app changing files or the Beets database behind their back.

## Positioning

Beets runs inside Web Manager as a pinned library (Beets 2.14.1, ADR 0006), against the operator's existing `config.yaml`, `musiclibrary.blb` and music folder. Every library change (move, rename, merge, delete, tag or artwork write) goes through a preview, apply, audit and rollback workflow, and ambiguous identity evidence goes to a review queue instead of being applied. The MusicBrainz release group is the album identity; AcoustID fingerprints and MusicBrainz are the primary evidence, and AI suggestions are optional and never decide identity on their own.

## Operating Context

- Deployed with Docker Compose on the operator's own server or NAS, opened in a browser at `http://<server-ip>:8337`; also used from a phone browser.
- Mounted folders: `/config` (Beets config and database), `/music` (read-write), `/downloads` (staging), `/web-manager-data` (the app's own state).
- Main screens: Library, Import (source, Find Music, Review Queue, Recently Imported), Playlists, Jobs (current activity, Clean All and other cleanup, job history), Settings (Config), System, Library Changes, Submissions, and the first-run Setup wizard.
- Long-running work (imports, scans, Clean All, syncs) runs as background jobs with logs, cancellation and retry state; the operator often leaves a job running and comes back.
- Integrations: MusicBrainz, AcoustID, Lidarr, slskd/Soulseek, Plex, Discogs, ListenBrainz and an optional AI provider. All are optional except MusicBrainz lookups for matching.

## Capabilities and Constraints

- Frontend: React and Next.js (static export served by the Flask backend), TypeScript, Tailwind CSS 4 and MUI.
- Library changes are never silent: destructive actions need confirmation and stronger evidence than suggestions.
- Secrets are never shown in the UI except through explicit reveal controls where the backend allows it.
- No maintainer-specific paths, hostnames or infrastructure in the product.
- Open: localization (the UI is English only today; no decision recorded).

## Brand Commitments

- The product name is **Beets Web Manager** ("Beets" alone in tight spaces).
- Error messages say what failed, what is affected, and what the operator can do next (AGENTS.md).

## Evidence on Hand

- Screenshots of the main screens in `screenshots/` (tour in `SCREENSHOTS.md`); they predate the current theme.
- No testimonials, user counts, benchmarks or press exist in the repository; do not invent any.

## Product Principles

1. **The library is the operator's.** Never change music files or the Beets database without a preview the operator can read and an audit entry they can roll back.
2. **Evidence before action.** Show why the app believes something (MusicBrainz, fingerprints) before asking the operator to accept it; doubt goes to review.
3. **Serve both the Beets veteran and the newcomer.** Defaults and onboarding for one must not hide or override configuration for the other.
4. **Say what to do next.** Every failure names what failed, what it affects, and the next step.

## Accessibility & Inclusion

Repository tests pin WCAG AA contrast for muted text (at least 4.5:1) and field borders (at least 3:1), a visible focus ring on every control, and layouts that work down to 320px wide without horizontal scrolling (issues #606, #607, #610, #619).
