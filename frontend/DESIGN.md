---
name: Trellis
description: A local-first workspace for coding and research agents.
colors:
  canvas: "#ffffff"
  ink: "#171717"
  rail: "#fafafa"
  line: "#eaeaea"
  focus: "#0072f5"
  info-light: "#0062d1"
  success-light: "#15803d"
  warning-light: "#925f00"
  danger-light: "#b42318"
  canvas-dark: "#0a0a0a"
  ink-dark: "#ededed"
  panel-dark: "#171717"
  line-dark: "#2e2e2e"
  focus-dark: "#0072f5"
  info-dark: "#52a8ff"
  success-dark: "#4ade80"
  warning-dark: "#ffb224"
  danger-dark: "#ff6369"
typography:
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, "Helvetica Neue", Arial, sans-serif'
    fontSize: "14px"
    letterSpacing: "-0.01em"
rounded:
  sm: "calc(var(--radius) * 0.6)"
  md: "calc(var(--radius) * 0.8)"
  base: "var(--radius)"
  lg: "var(--radius)"
  xl: "calc(var(--radius) * 1.4)"
---

# Design System: Trellis

## Overview

**Creative North Star: “The Precise Workbench” (provisional; inferred from the
existing interface and the user's request for more compact spacing).**

Trellis uses a restrained, neutral interface for sustained coding and research
work. Light and dark themes share the same monochrome structure, with color
reserved for focus and status. Preserve the current product identity when
refining individual screens.

**Key Characteristics:**

- Compact, deliberate spacing without crowding text or controls.
- Neutral surfaces with clear borders and restrained semantic color.
- System sans-serif typography and minimal surface decoration.

## Colors

The palette is neutral in both themes; blue marks focus and information, while
green, amber, and red distinguish status.

### Light theme

- **Canvas** (`#ffffff`): Main workspace and card surfaces.
- **Ink** (`#171717`): Primary text and high-emphasis actions.
- **Rail** (`#fafafa`): Sidebar surface.
- **Line** (`#eaeaea`): Dividers, borders, and quiet controls.
- **Focus** (`#0072f5`): Keyboard focus treatment.
- **Status** (`#0062d1`, `#15803d`, `#925f00`, `#b42318`): Information, success,
  warning, and destructive states.

### Dark theme

- **Canvas** (`#0a0a0a`): Main workspace and rail.
- **Ink** (`#ededed`): Primary text and high-emphasis actions.
- **Panel** (`#171717`): Cards, messages, and raised tonal surfaces.
- **Line** (`#2e2e2e`): Dividers and borders.
- **Focus** (`#0072f5`): Keyboard focus treatment.
- **Status** (`#52a8ff`, `#4ade80`, `#ffb224`, `#ff6369`): Information, success,
  warning, and destructive states.

## Typography

**Body Font:** System sans-serif stack (`-apple-system`, `BlinkMacSystemFont`,
`Segoe UI`, Inter, `Helvetica Neue`, Arial).

### Hierarchy

- **Body:** 14px with `-0.01em` letter spacing in the application shell.
- **Brand:** 15px, medium weight, tight line height.
- **Other roles:** Use the existing component styles; there is no separate
  display-font family.

## Layout

The application fills the viewport and places a 224px navigation rail beside a
flexible workspace. The workspace contains the top bar, conversation or settings
content, and the composer. On narrow screens, navigation becomes an overlay.

The rail uses 8px outer gutters and 8px row insets, aligning navigation icons,
section labels, and session markers at 16px. The Chat section starts 16px below
primary navigation. Session rows retain 13px text and 32px desktop targets;
coarse-pointer and mobile targets expand to at least 40px.

The shared stylesheet has no semantic spacing-token scale; components use
Tailwind's spacing utilities and local values. The user's stated preference is
to trim wasted padding and make spacing feel
intentional. Keep content readable and controls usable; reduce padding where it
improves hierarchy, not as a blanket rule.

## Elevation & Depth

The interface is flat by default. It separates surfaces with tonal changes and
subtle borders rather than a shared shadow scale. Hover, selected, pressed, and
focus states provide the primary depth and state cues.

## Shapes

The base radius is 12px, with smaller derived radii for controls and component-
specific larger corners. Prefer restrained rounded rectangles; use pill shapes
only where the existing component calls for them.

## Components

### Buttons and icon controls

- **Primary:** High-contrast neutral surface with a clear text or icon label.
- **Quiet controls:** Transparent at rest; use a subtle tonal surface on hover.
- **Focus:** Keep the 2px blue outline and 3px offset visible for keyboard use.

### Cards and fields

- Use the theme's canvas or panel surface with a 1px line.
- Keep field focus distinct with the existing blue focus token.
- Use padding proportionate to content density; avoid empty space that weakens
  grouping.

### Status and chat surfaces

- Reserve semantic colors for status and error states.
- User messages use a quiet neutral surface; assistant content remains integrated
  with the workspace.

## Do's and Don'ts

- Do improve spacing through clear grouping, alignment, and a consistent rhythm.
- Do preserve both light and dark theme relationships.
- Do keep keyboard focus visible.
- Don't shrink every padding value uniformly.
- Don't add new accent colors or heavy shadows without a product decision.
