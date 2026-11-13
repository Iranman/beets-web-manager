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
- The Library is browse-only, a simple overview: counts, search, scope, sort, A to Z and the artist/album navigation. No fix links or buttons to Import, Jobs or Missing IDs, no review pills, badges, stamps or notes. The background disk check polls silently; there is no status line for it. The one issue marker anywhere on the page is a missing track on the back cover: struck through in the soft ink with a small "missing" mark (`.is-missing`, `.sleeve-track-note`).
### Ops pages: Jobs & Cleanup, Workbench, Submissions (the `ops-*` block in `styles.css`)
- **One record shop, three counters.** Jobs & Cleanup is the listening station, Workbench (Import) the new-arrivals bin, Submissions the checking counter. Each page has one wooden surface, its focal object, and everything else sits in open composition: rules, spacing, an asymmetric split (`.ops-split`, a 15rem heading column beside the work from 64rem). No panel inside a panel, no stat tiles: counts are a `.ops-figures` row on a rule, label over value.
- **The Platter Rule (Jobs).** The running job sits on a record (`Platter` in `views/Jobs.tsx`): the album's cover is the centre label, a brass ring around the platter shows progress, and the tonearm is down while it plays. Its cover comes from the existing art endpoints only (`/api/albums/<id>/art`, `/api/release-art-cache/<rgid>`, `/api/disk-art`, via `albumArtSources`); with no art the label is plain brass. Idle, loading and error keep the empty platter with the arm lifted, so the station never collapses.
- **The one motion moment.** The record turns (5.4s a turn) only while its job is running, only under `prefers-reduced-motion: no-preference`, and pauses while the pointer or focus is on the station. Status text carries the meaning; the spin is decoration.
- **Sleeves** (`components/SleeveArt.tsx`): a fixed-size square, lazy-loaded with width and height set, falling through each art URL and then to an authored blank sleeve (sleeve paper with a record half out). Decorative (`alt=""`) because the album name is always printed beside it.
- **Wood on ops pages.** `.ops-station` uses `--ops-wood` (#20150e) with an interim self-hosted `feTurbulence` grain (`--ops-grain`) that only darkens. Text on it: `zinc-50` 16.6:1, `muted` 6.75:1. When the Library crate material (`.crate`, `walnut-*`, `--wood-grain`) lands on `main`, the station moves to it and `--ops-grain` is removed.
- **The bin (Workbench).** The page is the new-arrivals bin (`.ops-bin`): one walnut surface holding the latest six imports as sleeves (`.ops-arrivals`, art from the same `/api/jobs` read, names printed beside them) with the six sections standing in front as divider cards (`.ops-dividers`, the Headless UI tab list). The selected divider is pulled up out of the bin, the page's one motion moment, still under reduced motion. Below the bin the sections sit in open composition; Recently Imported is a ledger on rules with the album title in the display serif.
- **Controls** in ops pages are at least 40px tall, 44px on coarse pointers; filters are text with a brass underline when on (`.ops-filter.is-on`, `aria-pressed`).

### Playlists: mixtapes
- The page has two jobs, making playlists and syncing them, and is built around them. The focal point is the bench where a playlist is made: a mixtape J-card on the left takes the playlist's name as it is typed, and the add/import form sits beside it. Syncing to Plex has its own titled band with its status line, last-sync link and the Sync All button. Saved playlists stand in the walnut crate, one J-card per playlist.
- The J-card (`.mixtape`) is authored CSS: sleeve paper (`--color-brass-200` with the paper grain), two strips of tape colour across the top third, and the name lettered in the display serif (initials at row size). The tape tone comes from a hash of the name, from four token pairs. It is always `aria-hidden`; the name is printed beside it. Playlists carry no album ids, so there is no cover art to draw from; a cover mosaic needs that field from the backend first.
- Size the J-card in rem, never in percentages: percentage padding resolves against the row, not the card.
- Filters and track states are crate tabs (`.crate-tab`) with `aria-pressed`. Figures sit in runs of text, never in stat tiles or pills; state is a word in a state colour.
- No motion of its own: the Library's sleeve flip stays the app's one authored motion.

### System: diagnostics
- System is read-only first: one health line in the catalogue style (`.lib-title`, `.lib-catalog`, a `lib-review` pill that reads "All systems OK" in green or "N problems" in brass), the problem sentences, then Integrations and Paths as rows in the walnut crate (name, detail, state word in a state colour). No stat tiles or cards.
- Reference material (Beets plugins, Environment variables, Beets config) sits behind native `<details>` with a serif summary and a one-line hint. The first screen at 1440x900 shows the whole health picture.
- Editing Beets' config.yaml and sign-in lives in Settings (`/config#config-yaml`, `/config#security`); System links there.

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
