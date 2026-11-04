---
name: omaPOD
description: A podcast player that is a citizen of the Omarchy desktop; every value below is a role resolved from the active theme (reference instance Tokyo Night).
colors:
  ground: "#1a1b26"
  rail: "#13141c"
  dock: "#0e0e14"
  raised: "#24283b"
  ink: "#a9b1d6"
  ink-secondary: "#7e83a0"
  ink-tertiary: "#62667e"
  accent: "#7aa2f7"
  accent-ink: "#7aa2f7"
  on-accent: "#1a1b26"
  selection: "#292e42"
  danger: "#f7768e"
typography:
  display-large:
    fontFamily: "monospace"
    fontSize: "28px"
    fontWeight: 800
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  display:
    fontFamily: "monospace"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  heading:
    fontFamily: "monospace"
    fontSize: "16px"
    fontWeight: 700
  title:
    fontFamily: "monospace"
    fontSize: "14px"
    fontWeight: 700
  row-title:
    fontFamily: "monospace"
    fontSize: "13px"
    fontWeight: 700
  body:
    fontFamily: "monospace"
    fontSize: "12px"
    fontWeight: 400
  meta:
    fontFamily: "monospace"
    fontSize: "11px"
    fontWeight: 400
  caption:
    fontFamily: "monospace"
    fontSize: "10px"
    fontWeight: 400
  hero-figure:
    fontFamily: "monospace"
    fontSize: "48px"
    fontWeight: 800
rounded:
  window: "0px"
  control: "0px"
spacing:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "10px"
  xxl: "12px"
  huge: "18px"
  page: "24px"
components:
  button:
    backgroundColor: "rgba(169,177,214,0.04)"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    height: "28px"
    padding: "0 10px"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.control}"
    height: "28px"
  play-button:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.control}"
    size: "40px"
  chip:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    height: "24px"
    padding: "0 8px"
  chip-selected:
    backgroundColor: "rgba(169,177,214,0.18)"
    textColor: "{colors.ink}"
  episode-row:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    padding: "8px 24px"
  nav-row-selected:
    backgroundColor: "rgba(169,177,214,0.18)"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
  dock:
    backgroundColor: "{colors.dock}"
    textColor: "{colors.ink}"
    padding: "8px 12px"
---

# Design System: omaPOD

## Overview

**Creative North Star: "A citizen of the desktop"**

omaPOD looks like it shipped with Omarchy. It owns no colours of its own: every surface, text and accent is a role
taken from the active theme's `colors.toml`, the type is the system monospace that the bar uses, sizes follow the
shell's `[font] base-size`, and corners follow Hyprland's rounding. A theme switch re-colours the running app
within half a second. What remains to the app itself is structure, density and one signature: the episode that is
playing is framed like the focused Hyprland window, and chapters read like the bar's workspaces.

The reference values in the frontmatter are the Tokyo Night instance. With the White theme the same roles resolve
to a monochrome set (ground white, ink black, accent mid grey), which is why state never relies on hue alone.

**Key Characteristics:**
- Theme roles, never fixed colours; contrast is corrected per theme (secondary ink at least 4.5:1 on the ground).
- One family, the system monospace, on the shell's type scale.
- Square, hairline, flat: depth only where something floats (popovers, dialogs, toasts).
- Covers are the only imagery and the only free colour.

## Colors

The palette is Restrained by construction: neutrals from the theme plus its accent, with covers as the only
saturated fields.

### Primary
- **Accent** (`accent`, Tokyo Night `#7aa2f7`): progress, the playing frame, primary actions, the inbox mark and
  the current chapter. **Accent ink** is the same role pushed to 4.5:1 against the ground for text and links;
  **on-accent** is whichever of ground or ink reads best on an accent fill.

### Neutral
- **Ground** (`background`): window and page field.
- **Rail** (`dark_background`): the navigation rail; in light themes mixed 85 % toward the ground so monochrome
  themes stay calm.
- **Dock** (`darker_background`): the player dock, separated by a 22 % ink hairline.
- **Raised** (`lighter_background`): cover placeholders.
- **Ink, secondary ink, tertiary ink**: `foreground` at full strength, then mixed toward the ground and corrected
  to 4.6:1 and 3:1.
- **Selection** (`selection`): the current row in lists and selected text.
- **Danger** (`red`): errors and destructive actions, always with a label or outline.

### Named Rules
**The Borrowed Palette Rule.** No hex value is hard-coded in the stylesheet; a colour that is not a theme role,
or a role mixed with ink or ground, does not ship.
**The Shape-Before-Hue Rule.** Every state that has a colour also has a shape: the inbox dot, the star, the
check mark, the playing frame, the lit chapter box.

## Typography

**Display font:** the system monospace (fontconfig `monospace`, JetBrainsMono Nerd Font on Omarchy)
**Body font:** the same family

### Hierarchy
- **Hero figure** (800, 48 px at base 12): the one total on the statistics screen.
- **Display large** (800, 28 px): rarely; reserved for single-subject screens.
- **Display** (700, 24 px): page titles in the header, podcast and episode titles, Now Playing title.
- **Heading** (700, 16 px): section titles on Home and detail pages.
- **Title / row title** (700, 14 px and 13 px): list row titles, dock title.
- **Body** (400, 12 px): show notes, descriptions, controls.
- **Meta** (400, 11 px, tabular figures): dates, durations, sizes, counters.
- **Caption** (400, 10 px): legends and fine print.

All sizes scale with the shell's `[font] base-size` (12 px default).

### Named Rules
**The Shell Scale Rule.** Sizes come from the shell's multipliers (0.833, 0.917, 1, 1.083, 1.167, 1.333, 2,
2.333) times the user's base size; never a free-floating pixel size.
**The Tabular Meta Rule.** Times, counts and sizes use tabular figures so columns of numbers line up.
**The Literal Letters Rule.** Contextual alternates (`calt`) stay off: a programmer's font must not join `->` or
`!=` in a podcast title, and JetBrains Mono lays out text twice as fast without them.

## Layout

A three-part shell: a 220–272 px navigation rail on the left, the page in the middle, and the player dock across
the full width at the bottom. Pages open with a header row (display title, optional meta line, the page's own
actions at the right end), then filter chips, then a full-width list. Rows pad 24 px left and right, 8 px top
and bottom, and separate with a 12 % hairline instead of gaps.

Now Playing is the full player, so the dock never repeats it: the dock slides away while the page's play button
is on screen and slides back when the button scrolls out of view (long show notes, short windows).

Responsive behaviour follows tiling, not devices: below 760 sp the rail becomes an overlay (a toggle appears in
the header) and the dock drops volume and queue; below 540 sp the dock keeps only cover, transport, progress and
speed; below 400 sp speed goes too, Now Playing still has it. Detail pages (episode, Now Playing) clamp their
reading width to 920 px.

### Named Rules
**The Tiling Rule.** Every screen must work without clipping down to the window's minimum of 360 × 420 px; a 440 px
wide tile is the everyday case.

## Elevation & Depth

Flat. Surfaces sit on the ground without shadows; separation is done with the rail and dock tones and hairlines.
Only floating layers get depth: popovers, menus, dialogs and toasts carry a 1 px accent frame and a soft drop
(0 6–12 px, 18–32 px blur, black at 32 % in dark themes, 12 % in light ones), mirroring the shell's popups.

### Named Rules
**The Floating-Only Rule.** A shadow means "this floats above the window"; nothing in the page itself casts one.

## Shapes

Corners follow Hyprland's `decoration:rounding` (0 on this desktop); small controls take at most 6 px of it.
Lines are 1 px. Focus is a 1 px accent outline. The only round shape is the inbox dot.

## Components

### Buttons
- **Default:** 4 % ink fill, 1 px ink hairline at 40 %, 28 px tall; hover raises the fill to 8 %, pressed to 22 %,
  toggled-on to 18 % without an outline. Disabled at 45 % opacity.
- **Flat (toolbars, rail, dock):** transparent until hovered.
- **Primary and play:** solid accent with on-accent ink; the dock's play button is a 40 px square.

### Chips
Filter, period and preset chips: transparent with a 40 % hairline, 24 px tall; the active chip gets the 18 % fill
and a secondary-ink outline. Chips are a single-choice group.

### Cards / Containers
There are no cards. Home uses cover tiles (cover, two-line title, meta line, an overlaid play square) in
horizontal carousels; settings use the platform's boxed lists, outlined with a 22 % hairline.

### Inputs / Fields
4 % fill and 40 % hairline at rest; a focused field switches its hairline to accent. Placeholders use tertiary ink.

### Navigation
The rail lists screens with an icon, a label and a tabular counter (the inbox counter in accent ink). The selected
row gets the 18 % fill and a bold label with an accent icon. Podcasts follow below with 24 px covers.

### Episode row
Cover (52 px), an inbox dot, a two-line title, a meta line (podcast · date · duration or time left · size) with
state glyphs (downloaded, queued, favorite, video, played), a 2 px accent progress line when started, and two
actions: download (or cancel with percentage) and play. Played rows dim to tertiary ink; the playing row's title
turns accent ink and its cover gets the playing frame.

### Chapter bar (signature)
The progress bar is one segment per chapter with 3 px gaps. Played time fills in accent, the current chapter's
segment is lit with accent at 28 %, the rest is ink at 18 %. Hovering shows time and chapter title; click or drag
seeks; arrow keys step 5 s. Now Playing adds the chapters as a numbered strip in the bar's workspace rhythm:
done chapters dim, the current one solid accent, the rest outlined. Without chapters the bar is one segment.

### Playing frame (signature)
A 2 px accent ring around the cover of the episode that is loaded, wherever that cover appears (dock, rows,
Home tiles, Now Playing, the bar popup), like Hyprland's focused-window border.

## Do's and Don'ts

### Do:
- **Do** take every colour from a theme role and run new text colours through the contrast correction.
- **Do** pair every coloured state with a shape or glyph.
- **Do** keep page titles at display size in the header with the page's own actions at the right end.
- **Do** test new screens in Tokyo Night and White, and in a 440 px tile.

### Don't:
- **Don't** introduce rounded pills, stock libadwaita blue or card grids with shadows.
- **Don't** put a shadow on anything that does not float.
- **Don't** set a font family or size outside the shell scale.
- **Don't** use colour alone for a state.
