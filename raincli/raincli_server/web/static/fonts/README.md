# Web fonts

Every font here is licensed under the SIL Open Font License 1.1, and each family ships with its own licence file.

## Noto Serif Display 2.009 (`OFL-NotoSerifDisplay.txt`)
- **Files:** `noto-serif-display-{light,light-italic,regular}-v2009.woff2`, Latin subsets of the Light, Light Italic and Regular styles.
- **Used by:** the app and website pages (`app.css`) and the homepage (`landing.css`, Light and Light Italic).
- **Made with:** fontTools `pyftsubset` (WOFF2, no hinting; Basic Latin, Latin-1, common punctuation and arrows).

## JetBrains Mono 2.304 (`OFL-JetBrainsMono.txt`)
- **File:** `jetbrains-mono-regular-v2304.woff2`, a Latin subset of JetBrains Mono Regular.
- **Used by:** the homepage (`landing.css`), for labels, commands and metadata.
- **Source:** the official release, https://github.com/JetBrains/JetBrainsMono/releases/tag/v2.304
  - `JetBrainsMono-2.304.zip`, SHA-256 `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf`;
  - inside it, `fonts/ttf/JetBrainsMono-Regular.ttf`, SHA-256 `a0bf60ef0f83c5ed4d7a75d45838548b1f6873372dfac88f71804491898d138f`;
  - and `OFL.txt` (copied here as `OFL-JetBrainsMono.txt`), SHA-256 `30f0c136e3c88e422d0791acd97238870f9054a9729bc34cf2ff0d4ed8cac4ad`.
- **Made with:** fontTools 4.66.1 `pyftsubset`. There is no hinting and no layout features, so code ligatures never change how a command reads:

  ```
  pyftsubset JetBrainsMono-Regular.ttf --output-file=jetbrains-mono-regular-v2304.woff2 --flavor=woff2 \
    --no-hinting --layout-features='' \
    --unicodes="U+0020-007E,U+00A0-00FF,U+2010-2027,U+2030-203A,U+2190-2193,U+2212"
  ```

- **Result:** SHA-256 `506247dc473b0d7af392796a47f99563d3426ddf8165c0a4c8a5152c2c3b12fc`, 7,868 bytes.

## Versions in the filename
The version is part of each filename because static files are served with `Cache-Control: immutable`, and the CSS refers to fonts by path. A new font ships under a new name.
