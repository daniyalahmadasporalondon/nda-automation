# Design

Visual system for the Aspora NDA review tool. This file is also the **consolidation spec**: the target is to collapse `styles.css` + `repository.css` + `css/redesign.css` (+ the orphan tokens in `prototype.html`) into one system — `tokens.css → components.css → views/{review,repository,upload,admin}.css`.

## Theme

Light, neutral canvas with a single Aspora-purple accent. Flat surfaces (no glass / frost / backdrop-filter), soft diffuse shadows, restrained motion. Dark mode is staged in tokens (`:root[data-theme="dark"]`) but **not yet wired** to any toggle — light ships first; preserve the block, don't prune it.

## Color (canonical token set — survivors of consolidation)

Survivors = redesign's neutral light values + styles' semantic families + redesign's spacing scale. Drop prototype's `--pass/--review/--fail` aliases and the unused `--orange*`. Collapse the `--accent` alias into `--brand`.

**Surfaces**
`--bg-main:#fbfbfd; --bg-panel:#ffffff; --bg-soft:#f5f5f7; --bg-softer:#fafafc; --bg-chip:#eeeef2; --bg-doc:#ededf1; --border:#e7e7ec; --border-soft:#f0f0f4; --border-strong:#d8d8df;`

**Ink**
`--ink:#1d1d1f; --ink-soft:#56565b; --ink-muted:#86868b;`
⚠️ `--ink-muted` ≈ 3.5:1 on white — fails AA for text. For any text below ~16px use `--ink-soft` (≈ 7:1). Reserve `--ink-muted` for large/decorative only.

**Brand / accent** (single accent)
`--brand:#6028c8; --brand-bright:#7b46e0; --brand-strong:#4c1ba6; --brand-glow:rgba(96,40,200,.12); --accent-weak:rgba(96,40,200,.08); --accent-soft:rgba(96,40,200,.14); --accent-ring:rgba(96,40,200,.34);`

**Semantic / verdict** (keep ONE family)
`--green / -ink / -bg / -border` → PASS
`--amber / -ink / -bg / -border` → REVIEW
`--red / -ink / -bg / -border` → FAIL
(`--violet`, `--slate` retained where used.)

## Typography

**One family: Geist** (self-hosted variable, `assets/fonts/GeistVariable.woff2`) carries headings, UI, labels, body, and the document canvas. No display fonts in UI labels/data (product register).
DECISION PENDING: `PP Neue Corp Compact` is loaded but unused (wordmark is an image) — recommend dropping from UI; reserve only if a brand/marketing surface appears later.
Fixed rem scale (not fluid clamp). Step ratio ~1.125–1.2.

## Radii
`--r-xs:6; --r-sm:9; --r-md:13; --r-lg:16; --r-xl:20; --r-pill:9999;`

## Spacing
`--sp-1:4 --sp-2:8 --sp-3:12 --sp-4:16 --sp-5:20 --sp-6:24 --sp-7:32 --sp-8:40`

## Shadows
`--shadow-xs / -sm / -md / -paper / -page` — soft, diffuse, low-alpha neutral.

## Motion
150–250ms. `--ease: cubic-bezier(.4,0,.2,1)`; `--ease-spring: cubic-bezier(.34,1.56,.64,1)` for affordances only. Motion conveys state, not decoration. `prefers-reduced-motion`: crossfade / instant.

## Layout & key components

- **App shell:** flat topbar — wordmark left, tabs right (Repository · Upload · Review · Admin). No backdrop-filter (keeps text/icons crisp on HiDPI).
- **Review Studio:** 3-col grid — clause map · document canvas · inspector. Needs a responsive collapse below ~1270px (stack / drawer the inspector).
- **Verdict pill** is the load-bearing component: label + color, used in clause rows AND the inspector header. See PRODUCT principle 1. Tone class names (`.pass .review .fail/.prohibited` etc.) are written by JS as strings — rename tokens freely, never the tone classes.
- Extract one shared **fact-grid + pill + segmented-control** set (currently duplicated 3× across tabs under divergent names).

## Known debt (audit 2026-06-04)

~200–250 lines dead CSS; layered override system (redesign wins via `#reviewView` specificity in 271 places); silent Geist repoint; verdict not rendered in clause lane or inspector header; muted-text contrast; broken tab focus; studio grid not responsive.
