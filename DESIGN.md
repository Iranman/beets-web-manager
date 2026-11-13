---
name: Beets Web Manager
description: Self-hosted operator console for a Beets music library, in the warm-dark Record shelf theme.
colors:
  shelf-black: "#171310"
  shelf-walnut: "#201a16"
  shelf-oak: "#2a231d"
  shelf-oak-raised: "#342c25"
  shelf-edge: "#4b4137"
  field-border: "#8f8170"
  label-cream: "#f6f0e7"
  sleeve-muted: "#c4b5a2"
  sleeve-muted-2: "#a39481"
  muted-text: "#ab9d8b"
  brass: "#d39e3f"
  brass-bright: "#e2b25c"
  brass-deep: "#b5822b"
  brass-pale: "#f6dfae"
  brass-ink: "#1a140e"
  brass-patina: "#8f6420"
  focus-brass: "#f0c46a"
  danger: "#991b1b"
  danger-text: "#f87171"
  warning: "#f59e0b"
  success: "#4ade80"
  info: "#60a5fa"
typography:
  wordmark:
    fontFamily: "Iowan Old Style, Palatino Linotype, Palatino, Book Antiqua, Georgia, serif"
    fontSize: "1rem"
    fontWeight: 600
  headline:
    fontFamily: "Iowan Old Style, Palatino Linotype, Palatino, Book Antiqua, Georgia, serif"
    fontSize: "1.5rem"
    fontWeight: 600
    letterSpacing: "0em"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.43
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  pill: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
components:
  button-primary:
    backgroundColor: "{colors.brass}"
    textColor: "{colors.brass-ink}"
    rounded: "{rounded.md}"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.brass-bright}"
  button-secondary:
    backgroundColor: "#241e19"
    textColor: "#f8f1e7"
    rounded: "{rounded.md}"
    height: "32px"
  button-disabled:
    backgroundColor: "{colors.shelf-walnut}"
    textColor: "{colors.sleeve-muted-2}"
  input-field:
    backgroundColor: "{colors.shelf-walnut}"
    textColor: "{colors.label-cream}"
    rounded: "{rounded.md}"
  card:
    backgroundColor: "{colors.shelf-oak}"
    rounded: "{rounded.lg}"
    padding: "16px"
  nav-tab-active:
    backgroundColor: "#2b1e0b"
    textColor: "#faf6f0"
  chip:
    backgroundColor: "{colors.shelf-oak-raised}"
    textColor: "{colors.label-cream}"
    rounded: "{rounded.md}"
---

# Design System: Beets Web Manager

<!-- Recorded by `impeccable document` (scan mode) from frontend/src/styles.css, frontend/src/theme.ts
     and the shipped components. Descriptive language was chosen without an owner interview; the
     owner named the direction "Record shelf" (PRs #676, #678). -->

## Overview

**Creative North Star: "The Record Shelf"**

A late-evening listening room: dark wood shelving, cream sleeve labels, and one warm brass lamp that picks out what needs a hand. The interface is an operator console first. It is dense, tabular and calm, and the record-shop identity lives in a few precise places: the vinyl mark, the serif wordmark and page titles, and brass on the one thing the operator should act on next.

Surfaces are warm charcoal and brown-black, never blue-grey. Depth comes from stepping the walnut-to-oak surface ladder and a thin warm border, not from shadows. Brass is scarce on purpose: primary actions, the active tab, focus, and progress.

**Key Characteristics:**
- Warm dark only; there is no light theme.
- One accent (brass), used for action, selection, focus and progress.
- Serif display face for the wordmark and page titles; a system sans for everything an operator reads or clicks.
- Flat, bordered panels with tonal steps instead of elevation.
- Status colour (red, amber, green, blue) is reserved for job and evidence state, always paired with a text label.

## Colors

Warm wood neutrals with a single brass accent and a conventional semantic set for state.

### Primary
- **Brass** (`brass`): primary buttons, active navigation underline, progress bars, links that start the next step.
- **Bright Brass** (`brass-bright`): hover on brass surfaces, focused input labels.
- **Deep Brass** (`brass-deep`): pressed state of primary buttons.
- **Pale Brass** (`brass-pale`): the wordmark.
- **Brass Ink** (`brass-ink`): text on brass fills (at least 4.5:1).
- **Focus Brass** (`focus-brass`): the 2px focus ring on every control.

### Neutral
- **Shelf Black** (`shelf-black`): page background.
- **Walnut** (`shelf-walnut`): header bar, input fields, disabled buttons.
- **Oak** (`shelf-oak`): cards, panels, menus.
- **Raised Oak** (`shelf-oak-raised`): chips, hover rows, progress tracks.
- **Shelf Edge** (`shelf-edge`): panel borders and dividers.
- **Field Border** (`field-border`): input outlines (at least 3:1 against every surface).
- **Label Cream** (`label-cream`): primary text.
- **Sleeve Muted** (`sleeve-muted`, `sleeve-muted-2`, `muted-text`): secondary text, hints and metadata (`muted-text` is the floor: at least 4.5:1 on every surface).

### Semantic
- **Danger** (`danger`, `danger-text`): destructive buttons and failed state.
- **Warning** (`warning`): review-needed and caution state.
- **Success** (`success`): completed and verified state.
- **Info** (`info`): running and informational state.

**The One Lamp Rule.** Brass marks the one thing to do next. If two brass fills sit side by side, one of them is wrong.

**The Label-With-Colour Rule.** State colour never stands alone: every red, amber, green or blue chip carries its word ("Failed", "Running", "2 to review").

## Typography

**Display Font:** Iowan Old Style (with Palatino Linotype, Palatino, Book Antiqua, Georgia)
**Body Font:** Inter (with ui-sans-serif, system-ui, Segoe UI)

**Character:** A bookish serif for names and titles, set against a plain system sans that keeps dense tables readable. No web fonts are downloaded.

### Hierarchy
- **Wordmark** (600, 1rem): "Beets Web Manager" in the header.
- **Headline** (600, 1.5rem): the page title, one per screen.
- **Title** (600, 1rem): section headings inside a page.
- **Body** (400, 0.875rem): descriptions, table cells, messages.
- **Label** (600, 0.75rem): chips, field labels, metadata, timestamps.

**The Serif Is For Names Rule.** The display serif is for the wordmark and page and section titles only, never for buttons, labels, data or numbers.

## Layout

Single column inside a centred container (max 1536px) under a sticky 56px header. Pages stack bordered panels with 16px gaps; panels pad 12 to 16px. Controls sit on an 8px rhythm (gap-2 is the default). At phone width the primary tabs scroll sideways instead of wrapping, panels go full width with 12px side gutters, and nothing scrolls horizontally down to 320px.

## Elevation & Depth

Flat by default. Depth is tonal: Shelf Black, then Walnut, then Oak, then Raised Oak, each separated by a 1px Shelf Edge border. Shadows appear only on floating layers (menus, dialogs, toasts), as a soft black drop.

**The Flat Shelf Rule.** Panels at rest have a border and no shadow.

## Shapes

Gently rounded rectangles: 6px on buttons, inputs and chips, 8px on panels and cards, full pills only for count badges and status dots. The vinyl record mark is the one circular brand form.

## Components

### Buttons
- **Shape:** gently rounded (6px), at least 32px tall, label on one line.
- **Primary:** brass fill, brass-ink label. One per decision.
- **Secondary:** dark walnut fill with a light outline; hover lifts the border to bright brass.
- **Tertiary:** text only; hover adds a faint brass wash.
- **Danger / Warning:** deep red or deep amber fill with light text.
- **Disabled:** recessed walnut fill, soft border, muted label; looks the same in every variant.

### Chips
- **Style:** Raised Oak fill or a tinted outline in the state colour, 6px corners, 0.75rem semibold label.

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** Oak or Walnut on Shelf Black.
- **Shadow Strategy:** none (see Elevation).
- **Border:** 1px Shelf Edge.
- **Internal Padding:** 12 to 16px.

### Inputs / Fields
- **Style:** Walnut fill, Field Border outline, 6px corners, MUI small size.
- **Focus:** outline turns brass; label turns bright brass; the global 2px focus-brass ring shows on keyboard focus.
- **Disabled:** raised oak fill at reduced opacity.

### Navigation
- **Header:** a flat walnut bar closed by a 1px Shelf Edge border. No shadow, blur or gradient hairline: the header follows the Flat Shelf Rule like any panel.
- **Wordmark bay:** the record mark and wordmark sit in their own bay, closed on the right by a 1px divider from `sm` up, like the card at the front of a record bin.
- **Tabs:** text tabs (Library, Import, Playlists, Jobs) with a 2px brass underline and a faint brass wash when active.
- **System menu:** a full-height header button, a gear plus the word "System" (the word from `md` up). It holds Settings and Diagnostics (the System page at `/system`), then a divider and the app-level pair, Restart app (danger text) and Sign Out. On a page the menu holds (Settings, Diagnostics), the button wears the active-tab treatment (brass wash and the same 2px brass underline on the header edge), so every screen has a "you are here" cue.
- **Job status:** the Jobs tab carries separate pills for running (Info blue, matching the Jobs page) and failed (red) counts, both shown when both apply; each pill names its count for assistive technology and as a tooltip. Beside the System button, a status line spells the counts out ("2 running", "1 failed") at every width, outside the scrolling tabs, so a phone always shows it. Colour is never the only signal.

### Page frame
- One centred container (max 1536px), 12px gutters on phones and 16px from `sm`.
- **The One Title Rule:** each screen's single `h1` inside `main` is the display serif. The frame sets it in `styles.css`, so a view only chooses size and colour.

### Record Mark (signature)
- A 32px vinyl disc with grooves and a brass centre label beside the wordmark. Decorative; hidden from assistive technology.
- **The Tonearm Rule:** while any job runs, the header's mark shows a pale-brass tonearm resting on the grooves; it lifts when nothing runs. It is still, never spinning (no motion to pause), and the "N running" text and badge carry the meaning.

### Now Working bar (shell signature)
- A persistent, compact station docked at the foot of the shell (`components/NowWorkingBar.tsx`) showing the one running job. It is **presentational over job state the frontend already holds** — the shared `['jobs']` query via `useGlobalJobs` — so it adds no endpoint and never starts, cancels or changes a job.
- **Idle it renders nothing** (no footprint, never covers content, never steals focus). While a job runs it shows: a spinning record ring (the one app-wide use of the ring's spin, motion-gated), the album cover as a sleeve (`SleeveArt` from the existing art endpoints, a static record ring when there is none), the job title, the current step as a `.liner-label` with its progress text, and a **determinate brass progress bar** (`.nowbar-progress`, the polished-brass fill from `progress_percent`; an indeterminate brass sweep only when no percent is reported, reduced-motion falls back to a static half-lit track). The percent is also printed in words.
- **Expandable, not required.** A Details toggle reveals the last few live log lines (from `job.log`, already in the payload, cleaned through the shared `jobFeed` helpers); when no lines are present yet it links to Jobs & Cleanup instead. A **Jobs** link is always present. It is collapsed by default and dismissible.
- **Completion is quiet and never motion/colour alone.** When the running job finishes, the ring closes to a brass check (or an alert for a failure) beside a state word — "Completed" or "Finished with errors" — in a polite `role="status"` region, so assistive tech is told. It clears itself after a few seconds.
- **Solid walnut, no glass:** the bar sits over page content (including tables), so it uses a solid `walnut-900` surface — the Library liner band stays the app's one frosted-glass moment. Brass-on-walnut and text stay WCAG AA; controls keep a 44px hit target on coarse pointers; no horizontal overflow at 320/390/1440.

### Crate materials (Library; reusable on other pages)
- **Walnut wood** is the crate: `.crate` in `styles.css` sets `--color-walnut-900` with `--wood-grain`, an authored SVG `feTurbulence` grain inlined as a data URI (no image file, allowed by `img-src data:`). Tokens: `walnut-950` (bottom board), `walnut-900` (crate floor), `walnut-800` (row hover), `walnut-700` (divider tabs, rules on wood). Keep the grain at its current alpha: art and text lead. Text on wood is `zinc-100`/`zinc-300` (well over 4.5:1 on `walnut-700`); never put `zinc-600` or `muted` small text on `walnut-700` without checking.
- **Crate dividers** (`.crate-divider`, `.crate-tab`) are walnut tabs with a serif letter in `brass-200`; the selected tab gets a 2px brass top edge and `aria-pressed`. Below `sm` (640px) the A to Z tabs are one row that scrolls sideways, so the crate stays above the fold on a phone; from `sm` up they wrap.
- **Paper** (`--paper-grain` on cover faces, `--cardstock-grain` on the back cover) is only for sleeves. Never on panels or crates.
- **Sleeves have depth** (`.sleeve`): two offset edges of the sleeves behind and a soft drop. This is the one exception to the Flat Shelf Rule, because a sleeve is an object, not a panel.

### Imagery
- Album and artist art lead: cover tiles, a header shelf of leaning sleeves, and a blurred, tinted cover backdrop (`.lib-backdrop`) behind the artist header, bleeding to the content frame.
- Art comes only from the existing endpoints (`/api/albums/<id>/art`, `/api/disk-art`, artist image URL) with their fallbacks. No stock photos, no hotlinked images.
- No art: the authored sleeve placeholder (an SVG record half out of its sleeve) or, for artists, initials on a record label ringed by SVG grooves (`.lib-groove`, `.lib-label`).
- Every image is lazy, sits in a box of fixed size (no layout shift) and has alt text; decorative copies (the shelf, backdrops, the flip's front face) use `alt=""` and are hidden from assistive technology.

### Library signature: the sleeve flip
- Clicking (or Enter on) an album's sleeve pulls it out of the crate and turns it over (`.flip-card`, `.flip-inner`, `.flip-face`): 620ms, `cubic-bezier(0.16, 1, 0.3, 1)`, transform only, `rotateY(180deg)` with `backface-visibility: hidden`. Escape, Close or a click on the table turns it back (`.is-closing`, `sleeve-unflip`, 620ms ease-in) before the dialog closes. The sleeve is square (`aspect-ratio: 1`, at most 40rem and the viewport height) and sits on the dark wood (`.sleeve-table`).
- The back cover (`.flip-back`) is old card stock: `#e8dcc0` under `--cardstock-grain` (SVG fibre tooth plus slow foxing), an inset-shadow vignette and speckled edge wear (`::before`, masked by `--wear-speckle`). It is printed in brown ink: `--sleeve-ink #2b2118`, `--sleeve-ink-soft #5b4733`, `--sleeve-ink-mark #8a2c1d`, all at least 4.5:1 on the paper including the darker corners. Type: the display serif for the album title; a condensed sans (`--sleeve-font`) for the print; a mono (`--sleeve-mono`) for track numbers and times.
- When the album has art, the back is that same art (same URL as the front, so no new request), blurred, darkened and warmed (`.sleeve-back-art`), under a walnut scrim at 62% with the grain (`.has-art::after`); the print turns pale (`#f3ead6`, `#d9cbb0`, `#ffa58f`), above 4.5:1 even over white art. If the art fails or there is none, the plain paper is used.
- The tracklist is printed as SIDE A (first half) and SIDE B (second half): number, title, length right-aligned. Two columns when the sleeve is at least 28rem wide (a container query), one column below. Long lists scroll inside the paper area (`.sleeve-body`, focusable, named "Tracklist"); the sleeve keeps its square.
- The small print along the foot: artist, title, year, track count, total time and a catalogue number made from the release group ID (omitted when there is none).
- Focus moves to the back cover's title on open and returns to the sleeve on close. The front face is `inert` and `aria-hidden`. Under `prefers-reduced-motion` there is no rotation: the back crossfades in and out (180ms).
- The sleeve flip and the Cover Flow slide (below) are the Library's two authored motions, both started by the user. Nothing animates on load or at rest.

### Library Cover Flow (default view)
- `CoverFlow` (`frontend/src/components/CoverFlow.tsx`, `.cf-*` in `styles.css`) follows the react-coverflow interaction model. The stage is split into slots one sleeve wide; the active sleeve is flat and enlarged (scale 1.5) with its title on a walnut band along its foot; side sleeves sit a slot apart, turned 40deg about Y, smaller and further back. They slide with `transform 600ms ease`. Sleeves stand on the shelf (`transform-origin` at the bottom), and their reflections fade into a dark lacquered board (`.cf-floor`) over walnut grain.
- Visible side sleeves: 1 below 480px, 2 below 900px, 3 above. Two more on each side are rendered transparent so they can slide in, and nothing else is in the DOM.
- Navigation: arrows, Home, End and Enter on the focused region (`role="region"`, `aria-roledescription="carousel"`); the wheel or trackpad; drag or swipe (`touch-action: pan-y` keeps vertical page scrolling); a click on a side sleeve; the arrow buttons; the brass scrubber. Each sleeve is named "Album X by Y, 3 of 120" or "Artist X, 3 of 120", and a polite live region repeats the centred one.
- Reduced motion: a flat strip with no rotation and no transition.
- Going back: a brass "← All artists" text link (`.cf-back`) above the album flow; Escape or Backspace on the focused album flow (Escape inside the back-cover dialog only closes the dialog); browser or swipe Back, because the open artist is in the URL (`?artist=`). Each lands on the artist flow centred on that artist, and focus follows an in-page drill-in or back. In Cover Flow the artist header is a compact row.
- Artist sleeves use the artist image or the first album cover from the library payload (no per-artist request); without art, a lettered placard (`.cf-placard`) on sleeve paper over walnut.
- The crate grid stays as the alternate view behind the Cover Flow / Grid toggle (crate tabs with `aria-pressed`), remembered in `localStorage` under `library.view`.
- The Library is browse-only, a simple overview: counts, search, scope, sort, A to Z and the artist/album navigation. No fix links or buttons to Import, Jobs or Missing IDs, no review pills, badges, stamps or notes. The background disk check polls silently; there is no status line for it. The one issue marker anywhere on the page is a missing track on the back cover: struck through in the soft ink with a small "missing" mark (`.is-missing`, `.sleeve-track-note`). The one exception to "no links out" is the genuinely-empty-library state (below), where the empty shelf points to Import as onboarding, not as an action on records.

### Bolder Shelf: deeper walnut + bolder brass (foundation + Library preview)
A bold evolution inside the Record shelf world, through an Operate-tool filter: scanability and speed outrank expression, so motion stays short and reduced-motion-aware and glass is one measured moment. The foundation tokens are shared (reusable by every later view); the application below is on the Library view only for now.

**Brass, with a defined role.** Brass is reserved for exactly three jobs: the primary action, the active/selected state, and live progress. It gets real area — the primary MUI button is a solid brass fill with brass-ink text and a *polished* top-to-bottom sheen (`--bw-accent-polish`, a light-catching gradient); the LinearProgress bar is a polished brass fill; the active crate tab and the centred Cover Flow sleeve wear a brass edge; the selected row gets a brass left rail (`.brass-rail`). A deeper **patina** tone (`--bw-patina #8f6420`, in the frontmatter as `brass-patina`) edges the record ring and quiet brass work. Everything else stays warm neutral so brass reads as signal. Brass-on-walnut and text contrast stay WCAG AA; body text never sits on brass.

**Display serif for big counters too.** This direction extends the serif's reach from the wordmark/titles/names to the oversized liner-note counts (the stat band below). Body text, tables and dense data stay in the clean sans with tabular numerals; metadata gets a letterspaced small-caps label (`.liner-label`, used for format/bitrate/MBID). The display face is still the system serif stack (Iowan Old Style → Georgia): no web font is downloaded, so the no-download rule and the CSP are unchanged. (A downloadable display face is a possible later step, out of scope for this preview.)

**Walnut texture and directional light.** The app background (`body`) carries a fine static grain (`--app-grain`, an inlined SVG at ~4% alpha, CSP-safe), a warm brass light from the top, and a soft side-and-floor vignette — all linear gradients (no radial glow, #612), fixed to the viewport, no animation. Panels and the crate sit on top as slightly lighter, hairline-edged surfaces; tables stay on a flat panel colour, never directly on texture.

**The record ring (signature mark).** `components/RecordRing.tsx` is one vinyl centre-hole ring — patina body, brass centre label, one bright glint — reused as the empty-shelf mark, the loading spinner and the selected dot. Always decorative (hidden from assistive tech, per #612); `spinning` turns it only under `prefers-reduced-motion: no-preference`.

**Motion tokens.** One small set in `:root`: `--dur-hover: 120ms`, `--dur-press: 90ms`, `--dur-lift: 180ms`, `--ease-out`. Nothing on a frequent action exceeds `--dur-lift`, and the global reduced-motion rule caps every transition; the lift and the shimmer are motion-gated.

**Library application:**
- **Liner-note stat header (`.lib-liner`):** a page-top band of oversized display counts (artists, albums, tracks — each still one text node) over a blurred cover, with a `.liner-label` metadata line. It is the **one frosted-glass moment** ("warm glass"): a translucent walnut panel with `backdrop-filter` behind an `@supports` guard and a near-opaque solid fallback, dark enough that cream text clears AA over any cover. Glass lives on this frame only, never behind the lists. It collapses to one tight line on scroll (`useCollapseOnScroll`) and on narrow screens.
- **Album art as hero (`.lib-sleeve`, Library-scoped so Playlists' `.sleeve` is untouched):** larger covers on wider screens (phone grid stays two-up at 8rem), an inner edge highlight and a spine strip down the left, and on hover/focus a 4px lift (motion-gated) with a soft shadow and a 2px brass edge.
- **Designed placeholder:** the no-art sleeve keeps the album's initial in the display serif (`.lib-sleeve-initial`), beside the authored record-in-sleeve SVG; it keeps its "No cover art" accessible name.
- **Loading skeletons (`.lib-skeleton-*`):** cover squares and text lines under a slow, low-contrast warm shimmer (motion-gated), with a polite "Loading library…" status for assistive tech.
- **Character empty state (`.lib-empty`):** shown only when the library is genuinely empty — the record ring, "Nothing on this shelf yet…" in the serif, and a polished-brass "Go to Import" link as the one way in.
### Ops pages: Jobs & Cleanup, Workbench, Submissions (the `ops-*` block in `styles.css`)
- **One record shop, three counters.** Jobs & Cleanup is the listening station, Workbench (Import) the new-arrivals bin, Submissions the checking counter. Each page has one wooden surface, its focal object, and everything else sits in open composition: rules, spacing, an asymmetric split (`.ops-split`, a 15rem heading column beside the work from 64rem). No panel inside a panel, no stat tiles: counts are a `.ops-figures` row on a rule, label over value.
- **The Platter Rule (Jobs).** The running job sits on a record (`Platter` in `views/Jobs.tsx`): the album's cover is the centre label, a brass ring around the platter shows progress, and the tonearm is down while it plays. Its cover comes from the existing art endpoints only (`/api/albums/<id>/art`, `/api/release-art-cache/<rgid>`, `/api/disk-art`, via `albumArtSources`); with no art the label is plain brass. Idle, loading and error keep the empty platter with the arm lifted, so the station never collapses.
- **The one motion moment.** The record turns (5.4s a turn) only while its job is running, only under `prefers-reduced-motion: no-preference`, and pauses while the pointer or focus is on the station. Status text carries the meaning; the spin is decoration.
- **Sleeves** (`components/SleeveArt.tsx`): a fixed-size square, lazy-loaded with width and height set, falling through each art URL and then to an authored blank sleeve (sleeve paper with a record half out). Decorative (`alt=""`) because the album name is always printed beside it.
- **Wood on ops pages.** `.ops-station` uses `--ops-wood` (#20150e) with an interim self-hosted `feTurbulence` grain (`--ops-grain`) that only darkens. Text on it: `zinc-50` 16.6:1, `muted` 6.75:1. When the Library crate material (`.crate`, `walnut-*`, `--wood-grain`) lands on `main`, the station moves to it and `--ops-grain` is removed.
- **The bin (Workbench).** The page is the new-arrivals bin (`.ops-bin`): one walnut surface holding the latest six imports as sleeves (`.ops-arrivals`, art from the same `/api/jobs` read, names printed beside them) with the seven sections standing in front as divider cards (`.ops-dividers`, the Headless UI tab list). The selected divider is pulled up out of the bin, the page's one motion moment, still under reduced motion. Below the bin the sections sit in open composition; Recently Imported is a ledger on rules with the album title in the display serif.
- **The counter (Submissions).** The release being checked lies on one walnut surface (`.ops-counter`): its sleeve (the target's cover, then the album and release-group art endpoints, then the blank sleeve), the title in the display serif, and what will be submitted. Picking another release sets its sleeve down on the counter (the page's one motion moment, still under reduced motion). The steps are the `.ops-pipeline` numbered rule with `aria-current="step"`; the queue is a ledger of sleeves on rules with text filters; every other section is open on a rule.
- **Controls** in ops pages are at least 40px tall, 44px on coarse pointers; filters are text with a brass underline when on (`.ops-filter.is-on`, `aria-pressed`).
- **Bolder Jobs & Cleanup (`.jobs-bolder`, an appended scoped block in `styles.css`).** The listening station is raised into the Bolder Shelf world, scoped to `.jobs-bolder` so the shared `ops-*` surfaces Workbench and Submissions also use stay untouched. It reuses only what the foundation shipped: brass in its defined role — the live progress ring in polished brass (`--bw-accent-hover`) over an aged-patina track (`--bw-patina`), the playing Clean All phase as a filled brass track number, and the selected history row on the shared `.brass-rail`; the walnut room light with a hairline brass edge on the station; the figure labels as `.liner-label` small-caps over sans tabular values; the Clean All completion as the one oversized display-serif count; and the shared `RecordRing` as the station's loading spinner. The one measured frosted-glass moment is the job-details backdrop (`.jobs-modal-glass`) — an overlay, never behind the feed or the ledgers — with an `@supports` guard and a near-opaque walnut fallback. Behaviour is unchanged: every handler, confirmation (the Clean All checkpoint-discard `confirm`, cancel-job, clear-done), API call and test id is the same, and the record still turns only while a job runs, paused on hover/focus and under reduced motion. No horizontal overflow at 320/390/1440.

### Playlists: mixtapes
- The page has two jobs, making playlists and syncing them, and is built around them. The focal point is the bench where a playlist is made: a mixtape J-card on the left takes the playlist's name as it is typed, and the add/import form sits beside it. Syncing to Plex has its own titled band with its status line, last-sync link and the Sync All button. Saved playlists stand in the walnut crate, one J-card per playlist.
- Playlists does not download, import or repair. Run/Resume Pipeline, the download and import steps and single-track retries are on Workbench > Playlist Downloads (`/import?tab=playlists&playlist=<name>`); review-row repair and placement are on Jobs & Cleanup > Playlist Review Rows (`/jobs?tab=playlist-repair&playlist=<name>`). A saved playlist that needs that work links there ("Open in Workbench", "Open Review Rows in Jobs").
- The J-card (`.mixtape`) is authored CSS: sleeve paper (`--color-brass-200` with the paper grain), two strips of tape colour across the top third, and the name lettered in the display serif (initials at row size). The tape tone comes from a hash of the name, from four token pairs. It is always `aria-hidden`; the name is printed beside it. Playlists carry no album ids, so there is no cover art to draw from; a cover mosaic needs that field from the backend first.
- Size the J-card in rem, never in percentages: percentage padding resolves against the row, not the card.
- Filters and track states are crate tabs (`.crate-tab`) with `aria-pressed`. Figures sit in runs of text, never in stat tiles or pills; state is a word in a state colour.
- No motion of its own: the Library's sleeve flip stays the app's one authored motion.

### System: diagnostics
- System is read-only first: one health line in the catalogue style (`.lib-title`, `.lib-catalog`, a `lib-review` pill that reads "All systems OK" in green or "N problems" in brass), the problem sentences, then Integrations and Paths as rows in the walnut crate (name, detail, state word in a state colour). No stat tiles or cards.
- Reference material (Beets plugins, Environment variables, Beets config) sits behind native `<details>` with a serif summary and a one-line hint. The first screen at 1440x900 shows the whole health picture.
- Editing Beets' config.yaml and sign-in lives in Settings (`/config#config-yaml`, `/config#security`); System links there.

### Library Changes: the audit ledger
- Library Changes is a Workbench section (`views/LibraryChanges.tsx`, under the Workbench `h1`), so its title is an `h2` in `.ops-section-title`. It is a ledger, not a dashboard: open composition on rules, no stat tiles, no wood (the bin above it is the Workbench's one wooden surface).
- The filters (Status, Operation, Search) sit over the ledger. The count line reads "N transactions" with the page range on the right, or "Transactions unknown" when the list failed, never zero.
- Each row is an `.ops-ledger-row` button with `aria-pressed`: operation, then date and counts in tabular numerals, then the summary. The status is a word in a state colour (green done or rolled back, blue running or approved, amber preview, pending or partly rolled back, red failed, cancelled or Recovery Required), never a coloured chip alone.
- The selected transaction is the one bordered panel (Oak, 1px edge, no shadow): its operation as an `h3` in the display serif, summary and id, figures as an `.ops-figures` definition list (Date, Job, Dry run, Undo, the confidences), then the actions and the Export links on a rule. Preview changes, Log and Transaction Settings follow as open `h3` sections on rules; diffs are rows on soft rules, paths in mono.
- Actions and their confirmations are unchanged: the typed DELETE ALBUM FILES dialog, the rollback `confirm`, the server-owned cancel and rollback eligibility with their reasons. Approve is the one brass fill; nothing new lives here that has a home elsewhere.
- `.changes-page` (unlayered, in `styles.css`) gives the small MUI controls 40px, 44px on coarse pointers. It exists because MUI's emotion styles are unlayered and beat the layered `.ops-page` rule.

### Login: the front door
- A plain, trustworthy sign-in in a `main` landmark on Shelf Black: the record mark beside "Beets Web Manager" as the page's `h1` in the display serif (Pale Brass, like the wordmark), the tagline under it, then one Oak panel with a 1px edge and no shadow, blur or glow.
- Fields are 44px Walnut-on-black inputs with the Field Border and the global focus ring; 16px text below `sm` so phones do not zoom. Show/Hide is a full-height 44px text button inside the password field. Remember me is a 44px label row. Sign In is the one brass fill, full width, 44px.
- Errors appear above the fields in a red panel with `role="alert"`, led by "Sign-In Error:", carrying the server's message (wrong password, rate limit) unchanged.
- The flow, field ids, the default username and the messages are unchanged. No new fonts or images.
### Settings: the one editing page
- Settings is an Operate page: scanning and precision come before expression. It uses the ops frame: `.ops-page`, an `.ops-masthead` with the `ops-title` and the library counts as an `.ops-catalog` line (no stat tiles), then `.ops-split`.
- **The index.** The split's aside is a `<nav aria-label="Settings sections">` of plain `#id` links (MusicBrainz, Music format, Plugins, config.yaml, Security). From 64rem it is a vertical list on a left rule and stays in view (`.ops-split-aside` is sticky). Below 64rem it is one row of `.ops-filter` links that scrolls sideways inside itself, never the page. Every entry is also a deep link (`/config#plugins`, `#config-yaml`, `#security`, `#musicbrainz`, `#music-format`).
- **Deep links land.** The sections load at their own pace, so a hash target is scrolled to on mount and kept at the top while the page's height changes (a `ResizeObserver`), until the reader scrolls, clicks or types, or 8 seconds pass. Sections carry `scroll-mt-20` to clear the sticky header. The last section can only rise as far as the page's end allows.
- **Sections** are open on a rule (`SettingsSection` in `views/Config.tsx`): an `h2` in `ops-section-title`, a one-line hint, the section's actions on the right, then the controls. Sub-groups are `h3`s in the sans. No panel in a panel.
- **One wooden surface:** the preferred-format order, a `.crate` list with the rank in pale brass. Everything else is on the page background.
- Checkbox and radio rows are labels at least 40px tall (44px on coarse pointers), with the brass accent. Up and Down name their format ("Move FLAC up"). MusicBrainz gaps are `.ops-figures`, green at zero and amber otherwise, always with the number.
- No motion of its own.

## Do's and Don'ts

### Do:
- **Do** use the theme tokens (`graphite-*`, `zinc-*`, `brass-*`, `muted`, `field-border`) rather than raw hex in components.
- **Do** keep one brass primary action per decision; make the rest secondary or text buttons.
- **Do** give every screen one page title in the display serif.
- **Do** keep the 2px focus-brass ring visible on every interactive element.

### Don't:
- **Don't** reintroduce blue-grey or neutral grey surfaces; the shelf is warm.
- **Don't** use the serif on buttons, chips, numbers or table data.
- **Don't** convey job or evidence state by colour alone.
- **Don't** add shadows to panels at rest.
