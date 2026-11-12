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
- **Style:** text tabs in the header (Library, Import, Playlists, Jobs) with a 2px brass underline and a faint brass wash when active. A gear menu holds Settings, Library Changes, System, Restart and Sign out. A count badge on Jobs shows running (amber) or failed (red) jobs.

### Record Mark (signature)
- A 32px vinyl disc with grooves and a brass centre label beside the wordmark. Decorative; hidden from assistive technology.

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
