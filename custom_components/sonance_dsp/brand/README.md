# Brand images

Since HA 2026.3 a custom integration can ship its brand images here instead of in
`home-assistant/brands`. Local images take precedence, and HACS validation fails without
`icon.png`.

| File | Size | Status |
|---|---|---|
| `icon.png` | 256×256 | placeholder |
| `icon@2x.png` | 512×512 | placeholder |
| `logo.png` | max 512×512 | optional, absent |

The placeholders are a generated level-meter glyph on a dark rounded square, deliberately
generic. Replace them before any release that matters.

Do not use Sonance's trademarked logo without permission. A neutral amplifier, speaker or
level glyph avoids the question.
