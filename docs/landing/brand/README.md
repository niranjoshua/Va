# Vaticore brand kit

The mark is an eight-blade aperture: eight identical blades on a regular octagon,
each with two straight edges and one S-curve. It was traced from the approved
artwork, then rebuilt with exact geometry (one fitted blade, rotated in 45 degree
steps), so it is perfectly symmetric at any size.

## Colours

| Token | Hex | Use |
| --- | --- | --- |
| Ink | `#1D1A1E` | Mark, wordmark, text, buttons, dark product surfaces |
| Paper | `#FCF9E8` | App icon background, the call to action band |
| Tile | `#F5F4F0` | Feature tiles on white |
| Forecast teal | `#0E8F82` on light, `#1FA898` on dark | The forecast line and range only |
| Actual amber | `#B86F05` on light, `#C2811A` on dark | What actually happened, flagged gaps |

The logo itself is always one colour: ink on light, paper or white on dark.
Teal and amber are data colours. They are checked for colour-blind separation
and contrast on each surface, and are not used for decoration.

## Type

- Headings and body use the system stack `-apple-system, BlinkMacSystemFont,
  "SF Pro Display", "SF Pro Text", "Inter", ...`. On Apple devices this renders
  in SF Pro, Apple's own typeface. Everywhere else it falls back to Inter.
- SF Pro is licensed only for use on Apple platforms, so it is never embedded,
  outlined or used in the logo. The wordmark is Inter Display SemiBold, outlined
  to paths, which is the closest open equivalent.
- Numbers and code use the system monospace (SF Mono on Apple, JetBrains Mono
  elsewhere).

## Files

| File | Use |
| --- | --- |
| `vaticore-lockup.svg` / `.png` | Primary logo on light backgrounds |
| `vaticore-lockup-light.svg` / `.png` | On black, ink or dark photos |
| `vaticore-mark.svg`, `vaticore-mark-light.svg` | The mark alone |
| `vaticore-wordmark.svg` | Wordmark without the mark |
| `vaticore-icon.svg`, `vaticore-icon-512.png` | App icon, LinkedIn and social avatars |
| `vaticore-icon-dark.svg`, `vaticore-icon-dark-512.png` | App icon, dark variant |
| `favicon.svg`, `favicon-32.png`, `apple-touch-icon.png` | Browser and home screen icons |
| `og-image.png` | Link preview card (1200 x 630) |

## Rules

- Keep clear space around the lockup of at least half the mark's height.
- Do not rotate, recolour, outline, stretch or add effects to the mark.
- Minimum lockup height is 18 px on screen. Below that, use the mark or icon.
- The mark sits to the left of the wordmark, centred on the capital height.
