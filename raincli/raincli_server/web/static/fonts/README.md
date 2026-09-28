# Web fonts

`noto-serif-display-{light,light-italic,regular}-v2009.woff2` are Latin subsets of
Noto Serif Display 2.009 (Light, Light Italic, Regular), licensed under the SIL Open
Font License 1.1 (see `OFL.txt`). Made with fontTools `pyftsubset` (woff2, no hinting,
Basic Latin + Latin-1 + common punctuation and arrows).

The version is part of each filename because static files are served
`Cache-Control: immutable` and the CSS refers to fonts by path: ship a new
font under a new name.
