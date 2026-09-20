# Brand images

Since HA 2026.3 a custom integration can ship its own brand images here instead of opening
a PR against `home-assistant/brands`. Local images take precedence over the brands repo.

Needed before the first release:

| File | Size | Notes |
|---|---|---|
| `icon.png` | 256×256 | **Required by HACS.** Square, transparent background. |
| `logo.png` | max 512×512 | Optional. Wordmark; may be non-square. |
| `icon@2x.png` | 512×512 | Optional hDPI variant. |

Do not use Sonance's trademarked logo without permission — a neutral amplifier or speaker
glyph avoids the question entirely.
