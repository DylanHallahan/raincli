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

### Medium and Bold (the homepage's agent-teams animation, `demo-teams.css`)
- **Files:** `jetbrains-mono-medium-v2304.woff2` and `jetbrains-mono-bold-v2304.woff2`, the same Latin subset of JetBrains Mono Medium (500) and Bold (700). The animation draws its labels at 400, 500 and 700.
- **Source:** the same official v2.304 release zip (SHA-256 `6f6376c6…7bbf`, as above):
  - `fonts/ttf/JetBrainsMono-Medium.ttf`, SHA-256 `31c92d01a8a08528b718a43addf0ad3df0af2ca4b7b3290a452f70f358e14d3d`;
  - `fonts/ttf/JetBrainsMono-Bold.ttf`, SHA-256 `5590990c82e097397517f275f430af4546e1c45cff408bde4255dad142479dcb`.
- **Made with:** fontTools 4.67.0 `pyftsubset`, with the same options and `--unicodes` as Regular. Every character the animation draws (ASCII, `—`, `’`) is in that range; its check marks are drawn as paths.
- **Results:**
  - Medium: SHA-256 `993009a5ae2cd179441719fcbbe1eac957d54c2b3a825b1d8c937c2058c78440`, 7,964 bytes;
  - Bold: SHA-256 `674c78f292c218e61b97e725a5e48269344096e005325bb0a9ef417a95a953a6`, 7,988 bytes.

## Versions in the filename
The version is part of each filename because static files are served with `Cache-Control: immutable`, and the CSS refers to fonts by path. A new font ships under a new name.
