// The homepage's agent-teams animation, demo-teams v3 "The Gap" (marketing), ported from demo-teams-v3.html for the
// CSP: this file and static/demo-teams.css, the fonts self-hosted in static/fonts. The drawing, timing and colours are
// exactly as approved. One Canvas 2D pass per frame, a pure function of time: it plays only while on screen and the
// tab is visible, holds the composed end state under reduced motion, and has one small pause (hover, keyboard focus,
// or a tap on a touch screen; nothing visible at rest).
(() => {
"use strict";
const T = 24;                                   // loop length, seconds
// Production: no query-string hooks (the draft's ?t and ?theme are removed); the theme is prefers-color-scheme.
const HOLD = null;

const root = document.getElementById("adm");
const stage = root.querySelector(".adm-stage");
const canvas = stage.querySelector("canvas");
const ctx = canvas.getContext("2d");
const btn = root.querySelector(".adm-pp");

const SERIF = '"RC Serif Display", Georgia, serif';
const MONO = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';

/* ---------- palette ---------- */
const PAL = {
  light: { bg:"#f3eee4", card:"#fbf8f2", chrome:"#efe8dc", ink:"#1c1a16", muted:"#655d50", rule:"#d9cfbd",
    blue:"#1f5fd1", blueDeep:"#1d55c0", blueSoft:"#e8effc", green:"#1e7a3c", greenSoft:"#e2efe3",
    amber:"#b5650a", amberSoft:"#f7e8d0", coral:"#c8492e", violet:"#6b4cc6", teal:"#0e7a84", magenta:"#b3367b",
    shadow:[70,52,24], shadowK:1, onAccent:"#ffffff", logo:"#1f5fd1" },
  dark: { bg:"#13120f", card:"#1d1b17", chrome:"#24211c", ink:"#efe8db", muted:"#a2998a", rule:"#38322a",
    blue:"#7aa7ff", blueDeep:"#1a4aa8", blueSoft:"#1e2a44", green:"#6fcf8e", greenSoft:"#1b2d21",
    amber:"#f0b45a", amberSoft:"#352914", coral:"#ff8a6e", violet:"#b39bff", teal:"#5fd0d6", magenta:"#f08cc0",
    shadow:[0,0,0], shadowK:2.6, onAccent:"#13120f", logo:"#1f5fd1" }
};
let pal = PAL.light;
const mqDark = matchMedia("(prefers-color-scheme: dark)");
function pickTheme() {
  pal = mqDark.matches ? PAL.dark : PAL.light;
}

/* ---------- easing ---------- */
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const seg = (t, a, b) => clamp((t - a) / (b - a), 0, 1);
const lerp = (a, b, k) => a + (b - a) * k;
const E = {
  inQuad: x => x * x,
  outQuad: x => 1 - (1 - x) * (1 - x),
  inCubic: x => x * x * x,
  outCubic: x => 1 - Math.pow(1 - x, 3),
  outQuart: x => 1 - Math.pow(1 - x, 4),
  inOutCubic: x => x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2,
  inOutQuint: x => x < .5 ? 16 * x ** 5 : 1 - Math.pow(-2 * x + 2, 5) / 2,
  inOutSine: x => -(Math.cos(Math.PI * x) - 1) / 2,
  outExpo: x => x >= 1 ? 1 : 1 - Math.pow(2, -10 * x),
};
// damped spring step response 0 -> 1
function spring(u, zeta = .6, w = 14) {
  if (u <= 0) return 0;
  const wd = w * Math.sqrt(1 - zeta * zeta);
  return 1 - Math.exp(-zeta * w * u) * (Math.cos(wd * u) + (zeta * w / wd) * Math.sin(wd * u));
}
// decaying wobble that starts at 1 (for squash / nods)
const wobble = (u, k = 7, f = 15) => u < 0 ? 0 : Math.exp(-k * u) * Math.cos(f * u);

let STILL = false;            // reduced-motion end state: every transition resolved
const grow = (t, a, b, ease = E.outCubic) => STILL ? (t >= a ? 1 : 0) : ease(seg(t, a, b));

/* ---------- seeded randomness ---------- */
function rng(seed) {
  return () => {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let r = Math.imul(seed ^ seed >>> 15, 1 | seed);
    r = r + Math.imul(r ^ r >>> 7, 61 | r) ^ r;
    return ((r ^ r >>> 14) >>> 0) / 4294967296;
  };
}

/* ---------- geometry: bezier paths ---------- */
function bez(p0, c1, c2, p1) { return { p0, c1, c2, p1 }; }
function bezAt(b, u) {
  const v = 1 - u;
  return {
    x: v*v*v*b.p0.x + 3*v*v*u*b.c1.x + 3*v*u*u*b.c2.x + u*u*u*b.p1.x,
    y: v*v*v*b.p0.y + 3*v*v*u*b.c1.y + 3*v*u*u*b.c2.y + u*u*u*b.p1.y
  };
}
function makePath(beziers) {
  const pts = [], cum = [];
  let len = 0;
  beziers.forEach((b, bi) => {
    for (let i = (bi ? 1 : 0); i <= 48; i++) {
      const p = bezAt(b, i / 48);
      if (pts.length) len += Math.hypot(p.x - pts[pts.length - 1].x, p.y - pts[pts.length - 1].y);
      pts.push(p); cum.push(len);
    }
  });
  return { pts, cum, len };
}
function pathAt(path, d) {
  const { pts, cum } = path;
  d = clamp(d, 0, path.len);
  let lo = 0, hi = cum.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (cum[m] < d) lo = m; else hi = m; }
  const k = (d - cum[lo]) / Math.max(1e-6, cum[hi] - cum[lo]);
  return { x: lerp(pts[lo].x, pts[hi].x, k), y: lerp(pts[lo].y, pts[hi].y, k),
           a: Math.atan2(pts[hi].y - pts[lo].y, pts[hi].x - pts[lo].x) };
}
function strokePath(path, d0, d1) {
  const { pts, cum } = path;
  if (d1 <= d0) return;
  ctx.beginPath();
  const a = pathAt(path, d0); ctx.moveTo(a.x, a.y);
  for (let i = 0; i < pts.length; i++) if (cum[i] > d0 && cum[i] < d1) ctx.lineTo(pts[i].x, pts[i].y);
  const b = pathAt(path, d1); ctx.lineTo(b.x, b.y);
  ctx.stroke();
}

/* ---------- view / layout ---------- */
const view = { W: 0, H: 0, dpr: 1, s: 1, L: null, mode: "" };
let curScale = 1;

function buildLayout(mode) {
  const desk = mode === "desk";
  const L = { mode, desk };
  L.D = desk ? { w: 1180, h: 720 } : { w: 390, h: 800 };
  L.sz = desk
    ? { LH: 14, barH: 5, pillH: 30, fileH: 26, vg: 3, chip: 13, name: 46, title: 78, tb: 26, pad: 14, padT: 12, padB: 12, r: 11, label: 16 }
    : { LH: 11.5, barH: 4, pillH: 27, fileH: 24, vg: 3, chip: 12, name: 34, title: 46, tb: 22, pad: 10, padT: 9, padB: 9, r: 9, label: 13 };
  const S = L.sz;
  if (desk) {
    L.title = { x: 56, lines: [{ words: ["Agent", "Direct", "Messaging"], y: 336 }, { words: ["for", "your", "team"], y: 430, it: true }] };
    L.names = { maya: { x: 56, y: 226 }, jon: { x: 646, y: 226 } };     // Jon aligns with his enclosure and its label
    L.terms = {
      A:  { x: 56,  y: 250, w: 300, h: 350, accent: "violet", big: true },
      M2: { x: 370, y: 250, w: 150, h: 168, accent: "teal" },
      M3: { x: 370, y: 432, w: 150, h: 168, accent: "magenta" },
      J1: { x: 824, y: 250, w: 300, h: 350, accent: "coral", big: true },
      J2: { x: 660, y: 250, w: 130, h: 168, accent: "violet" },     // narrower: a wider gap for Jon's own hand-off
      J3: { x: 660, y: 432, w: 130, h: 168, accent: "teal" },
    };
    L.logo = { x: 590, y: 114, s: 58 };
    L.encl = { x: 644, y: 234, w: 496, h: 382, r: 22 };
    L.setup = { x: 646, y: 649 };
  } else {
    L.title = { x: 18, lines: [{ words: ["Agent", "Direct"], y: 352 }, { words: ["Messaging"], y: 408 }, { words: ["for", "your", "team"], y: 464, it: true }] };
    L.names = { maya: { x: 16, y: 56 }, jon: { x: 16, y: 502 } };
    L.terms = {
      A:  { x: 16,  y: 72,  w: 214, h: 214, accent: "violet", big: true },
      M2: { x: 240, y: 72,  w: 134, h: 102, accent: "teal" },
      M3: { x: 240, y: 184, w: 134, h: 102, accent: "magenta" },
      // mirrored like desktop: Jon's small agents under his name, the inbox agent on the outside
      J1: { x: 160, y: 528, w: 214, h: 214, accent: "coral", big: true },
      J2: { x: 16,  y: 528, w: 134, h: 102, accent: "violet" },
      J3: { x: 16,  y: 640, w: 134, h: 102, accent: "teal" },
    };
    L.logo = { x: 195, y: 400, s: 50 };
    L.encl = { x: 7, y: 516, w: 376, h: 238, r: 18 };
    L.setup = { x: 16, y: 778 };
  }
  // derived terminal geometry
  const ENTER = { A: 3.6, M2: 3.71, M3: 3.82, J1: 3.75, J2: 3.86, J3: 3.97 };
  for (const [id, m] of Object.entries(L.terms)) {
    m.id = id; m.enter = ENTER[id];
    m.bx = m.x + S.pad; m.bw = m.w - 2 * S.pad;
    m.by = m.y + S.tb + S.padT; m.bh = m.h - S.tb - S.padT - S.padB;
    m.pillW = Math.round(m.bw * (m.big ? .86 : .8));
  }
  buildLines(L);
  buildRoutes(L);
  buildCams(L);
  return L;
}

/* ---------- terminal content ---------- */
const TOKENS = ["blue", "violet", "teal", "green", "magenta", "coral"];
function segsFor(r, kind, bw, accent) {
  const out = [];
  if (kind === "cmd") {
    out.push({ g: "chev", x: 0, c: accent });
    let x = 13, w = bw * (.16 + r() * .16); out.push({ x, w, c: accent, a: .9 }); x += w + 6;
    w = bw * (.08 + r() * .22); if (x + w < bw * .92) out.push({ x, w, c: "ink", a: .32 });
  } else if (kind === "ok") {
    out.push({ g: "check", x: 0, c: "green" });
    out.push({ x: 13, w: bw * (.22 + r() * .3), c: "ink", a: .3 });
  } else {
    let x = 13; const n = 1 + Math.floor(r() * 3);
    for (let i = 0; i < n; i++) {
      const w = bw * (.08 + r() * .26);
      if (x + w > bw * .94) break;
      const tok = r() < .3;
      out.push({ x, w, c: tok ? TOKENS[Math.floor(r() * TOKENS.length)] : "ink", a: tok ? .78 : .2 });
      x += w + 5;
    }
  }
  return out;
}
function pushBg(lines, r, m, t0, t1, gmin, gmax, S) {
  let t = t0 + r() * gmax;
  while (t < t1) {
    const p = r();
    const kind = p < .22 ? "cmd" : p < .3 ? "ok" : "out";
    lines.push({ kind, t, h: S.LH, segs: segsFor(r, kind, m.bw, m.accent) });
    t += gmin + r() * (gmax - gmin);
  }
}
function finish(lines) {
  for (const ln of lines) { if (ln.g0 == null) { ln.g0 = ln.t; ln.g1 = ln.t + .09; } }
  lines.sort((a, b) => a.g0 - b.g0);
  return lines;
}
function buildLines(L) {
  const S = L.sz, Tm = L.terms;
  const pillLine = S.pillH + 2 * S.vg, fileLine = S.fileH + 2 * S.vg;
  // A — Maya's agent that asks
  {
    const m = Tm.A, r = rng(11), lines = [];
    const K = Math.max(7, Math.floor(.56 * m.bh / S.LH));
    const late = [4.5, 5.1, 5.6, 6.2, 6.8, 7.4, 7.85];
    for (let i = 0; i < K; i++) {
      const t = i < K - late.length ? -8 + i * .7 : late[i - (K - late.length)];
      const kind = i === 0 || i === K - 1 || i === 4 ? "cmd" : (r() < .14 ? "ok" : "out");
      lines.push({ kind, t, h: S.LH, segs: segsFor(r, kind, m.bw, m.accent) });
    }
    lines.push({ id: "q", kind: "q", t: 8.25, h: pillLine, g0: 8.25, g1: 8.4 });
    lines.push({ id: "afile", kind: "file", t: 20.2, h: fileLine, g0: 20.2, g1: 20.28, show: 20.27 });
    // keep the docked answer in view after the scrollback resumes
    const used = K * S.LH + pillLine + fileLine;
    const maxScroll = K * S.LH - S.LH * .6;
    const n = Math.max(3, Math.floor((m.bh + maxScroll - used) / S.LH));
    for (let i = 0; i < n; i++) {
      const fast = i < 6;
      const t = fast ? 20.55 + i * .16 : 21.55 + (i - 6) * .38;
      if (t > 22.55) break;
      const kind = fast ? (i === 3 ? "cmd" : "ok") : (i % 3 === 0 ? "cmd" : "out");
      lines.push({ kind, t, h: S.LH, segs: segsFor(r, kind, m.bw, m.accent) });
    }
    m.lines = finish(lines);
  }
  // J1 — Jon's inbox agent (busy all along: it receives instantly)
  {
    const m = Tm.J1, r = rng(23), lines = [];
    pushBg(lines, r, m, -12, 12.2, .5, 1.05, S);
    lines.push({ id: "in", kind: "in", t: 12.92, h: pillLine, g0: 12.38, g1: 12.72, show: 13.3 });
    for (const t of [13.55, 13.8]) lines.push({ kind: "out", t, h: S.LH, segs: segsFor(r, "out", m.bw, m.accent) });
    // the hand-off line: a short command, then the grey arrow the sub-slip leaves from
    lines.push({ id: "hand", kind: "hand", t: 14.1, h: S.LH * 1.3, segs: [{ g: "chev", x: 0, c: m.accent }, { x: 13, w: m.bw * .15, c: m.accent, a: .9 }] });
    // the reply opens its space as the file comes back from Jon's other agent; the file lands straight onto it,
    // so reply + file exist once. The line closes after the slip has cleared the window.
    lines.push({ id: "reply", kind: "reply", t: 17.08, h: pillLine + fileLine, g0: 16.55, g1: 16.8, chipAt: 17.15, c0: 18.2, c1: 18.6 });
    for (const t of (L.desk ? [18.8, 19.4, 20.6, 21.5, 22.3] : [19.4, 21.5])) {
      const kind = t === 20.6 ? "cmd" : "out";
      lines.push({ kind, t, h: S.LH, segs: segsFor(r, kind, m.bw, m.accent) });
    }
    m.lines = finish(lines);
  }
  // J2 — another of Jon's agents (Jon's own setup)
  {
    const m = Tm.J2, r = rng(37), lines = [];
    pushBg(lines, r, m, -12, 14.3, .55, 1.25, S);
    const subH = Math.round(S.LH * 1.8), foH = Math.round(S.LH * 1.5);
    lines.push({ id: "sub", kind: "sub", t: HS.land, h: subH, g0: 14.9, g1: HS.drop, show: HS.hand });
    const quick = clamp(Math.floor((m.bh - subH - foH) / S.LH) - 1, 1, 3);
    for (let i = 0; i < quick; i++) {
      const kind = i === quick - 1 ? "ok" : "out";
      lines.push({ kind, t: 15.55 + i * .13, h: S.LH, segs: segsFor(r, kind, m.bw, m.accent) });
    }
    lines.push({ id: "fo", kind: "fileout", t: 15.95, h: foH });
    // afterwards it keeps working, but never so much that the hand-off scrolls out of view
    const room = Math.max(0, Math.floor((m.bh - subH - foH - quick * S.LH) / S.LH));
    for (let i = 0; i < room; i++) lines.push({ kind: "out", t: 17.7 + i * 1.6, h: S.LH, segs: segsFor(r, "out", m.bw, m.accent) });
    m.lines = finish(lines);
  }
  for (const [id, seed] of [["M2", 51], ["M3", 67], ["J3", 79]]) {
    const m = Tm[id], r = rng(seed), lines = [];
    pushBg(lines, r, m, -12, 24, .45, 1.2, S);
    m.lines = finish(lines);
  }
}

// where every line of a terminal sits at time t (terminal-local, before entry offset)
function termLayout(m, t) {
  const items = []; let cum = 0;
  for (const ln of m.lines) {
    if (t < ln.g0) break;
    const g = grow(t, ln.g0, ln.g1);
    const c = ln.c0 == null ? 0 : grow(t, ln.c0, ln.c1, E.inOutCubic);   // a line that leaves (the sent reply)
    items.push({ ln, y: cum, g });
    cum += ln.h * g * (1 - c);
  }
  return { items, scroll: Math.max(0, cum - m.bh) };
}
function entry(m, t) {
  const t0 = m.enter;
  if (STILL) return { dy: 0, a: 1, sc: 1 };
  const p = spring(t - t0, .72, 12.5);
  return { dy: (1 - p) * 48, a: E.outQuad(seg(t, t0, t0 + .3)), sc: .965 + .035 * p };
}
function lineTop(m, id, t) {
  const { items, scroll } = termLayout(m, t);
  const it = items.find(i => i.ln.id === id);
  const e = entry(m, t);
  return it ? m.by + it.y - scroll + e.dy : m.by;
}
// pill rect (top-left) for a pill-type line
function pillRect(m, id, t) {
  const S = view.L.sz;
  const y = lineTop(m, id, t) + S.vg;
  return { x: m.bx, y, w: m.pillW, h: S.pillH, cx: m.bx + m.pillW / 2, cy: y + S.pillH / 2 };
}

/* ---------- routes ---------- */
function buildRoutes(L) {
  const { A, J1 } = L.terms, lg = L.logo, S = L.sz;
  const holeCx = A.bx + A.pillW / 2, slotCx = J1.bx + J1.pillW / 2;
  const R = {};
  if (L.desk) {
    // endpoints sit on the right of Maya's window so a slip's full width clears the "Maya" name
    // on Jon's window both endpoints sit between the "Jon" name and the inbox chip, so slips clear both
    const o = 7, P0 = { x: A.x + A.w * .83, y: A.y }, E0 = { x: J1.x + J1.w * .36, y: J1.y };
    const R0 = { x: J1.x + J1.w * .33, y: J1.y }, R1 = { x: A.x + A.w * .95, y: A.y };
    R.out1 = bez(P0, { x: P0.x, y: P0.y - 112 }, { x: lg.x - 150, y: lg.y - o }, { x: lg.x - 8, y: lg.y - o });
    R.out2 = bez({ x: lg.x + 8, y: lg.y - o }, { x: lg.x + 175, y: lg.y - o }, { x: E0.x, y: E0.y - 172 }, E0);
    R.ret1 = bez(R0, { x: R0.x, y: R0.y - 172 }, { x: lg.x + 175, y: lg.y + o }, { x: lg.x + 8, y: lg.y + o });
    R.ret2 = bez({ x: lg.x - 8, y: lg.y + o }, { x: lg.x - 110, y: lg.y + o }, { x: R1.x, y: R1.y - 60 }, R1);
    R.vA = -1;                       // direction from A towards RainCLI
  } else {
    const o = 6, ab = A.y + A.h;
    // both endpoints left of the inbox chip so slips (and the reply's hanging file) clear it
    // Maya-side endpoints keep a full slip width inside the 390px frame
    const P0 = { x: holeCx - 6, y: ab }, E0 = { x: J1.x + J1.w * .17, y: J1.y };
    const R0 = { x: J1.x + J1.w * .25, y: J1.y }, R1 = { x: holeCx + 24, y: ab };
    R.out1 = bez(P0, { x: P0.x, y: P0.y + 58 }, { x: lg.x - o, y: lg.y - 74 }, { x: lg.x - o, y: lg.y - 8 });
    R.out2 = bez({ x: lg.x - o, y: lg.y + 8 }, { x: lg.x - o, y: lg.y + 74 }, { x: E0.x, y: E0.y - 58 }, E0);
    R.ret1 = bez(R0, { x: R0.x, y: R0.y - 58 }, { x: lg.x + o, y: lg.y + 74 }, { x: lg.x + o, y: lg.y + 8 });
    R.ret2 = bez({ x: lg.x + o, y: lg.y - 8 }, { x: lg.x + o, y: lg.y - 74 }, { x: R1.x, y: R1.y + 58 }, R1);
    R.vA = 1;
  }
  L.routes = R;
}

/* ---------- camera ---------- */
function fit(L, x0, y0, x1, y1) {
  const z = Math.max(1, Math.min(L.D.w / (x1 - x0), L.D.h / (y1 - y0)));
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, z };
}
function buildCams(L) {
  const D = L.D, wide = { cx: D.w / 2, cy: D.h / 2, z: 1 };
  let maya, jon;
  if (L.desk) {
    maya = fit(L, 0, 176, 536, 626);
    maya = { right: L.encl.x - 10, cy: maya.cy, z: maya.z };   // right edge pinned short of Jon's enclosure
    // RainCLI tile + Jon's setup. Left edge pinned in the gap between Maya's big and small windows
    // (resolved against the live aspect ratio in camAt) so no window is ever sliced by the frame.
    jon = { left: 363, cy: 425, z: 1.35 };         // Jon's setup, RainCLI tile out of shot; left edge in the gap after Maya's big window
  } else {
    maya = fit(L, 6, 18, 240, 446);               // Maya's agent with the RainCLI tile wholly in shot below it
    jon = fit(L, 0, 0, 390, 800);
  }
  L.cams = [
    [0, 6.6, wide, wide, E.inOutCubic],
    [6.6, 7.6, wide, maya, E.inOutQuint],
    [10.4, 11.4, maya, wide, E.inOutCubic],
    [13.2, 14.2, wide, jon, E.inOutQuint],
    [17.35, 18.25, jon, wide, E.inOutCubic],
    [19.05, 19.95, wide, maya, E.inOutCubic],
    [21.55, 22.65, maya, wide, E.inOutCubic],
  ];
}
function resolveCam(c) {
  if (c.left == null && c.right == null) return c;
  const visW = view.W / (view.s * c.z);
  return { cx: c.left != null ? c.left + visW / 2 : c.right - visW / 2, cy: c.cy, z: c.z };
}
function camAt(t) {
  const C = view.L.cams;
  let k = C[0];
  for (const c of C) if (t >= c[0]) k = c;
  const p = k[4](seg(t, k[0], k[1]));
  const a = resolveCam(k[2]), b = resolveCam(k[3]);
  return { cx: lerp(a.cx, b.cx, p), cy: lerp(a.cy, b.cy, p), z: a.z * Math.pow(b.z / a.z, p) };
}

/* ---------- drawing helpers ---------- */
function rr(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, Math.min(r, h / 2, w / 2)); }
function shadow(blur, oy, a) {
  const [r, g, b] = pal.shadow;
  ctx.shadowColor = `rgba(${r},${g},${b},${Math.min(.85, a * pal.shadowK)})`;
  ctx.shadowBlur = blur * curScale; ctx.shadowOffsetX = 0; ctx.shadowOffsetY = oy * curScale;
}
function noShadow() { ctx.shadowColor = "transparent"; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0; }
// Safari has no Canvas 2D filter: feature-detect it, and without it the blur-ins fall back to their fade and rise.
const CAN_BLUR = (() => {
  try { const c = document.createElement("canvas").getContext("2d"); c.filter = "blur(1px)"; return c.filter === "blur(1px)"; }
  catch (_) { return false; }
})();
if (!CAN_BLUR) root.classList.add("adm-no-blur");
function blur(px) { if (CAN_BLUR) ctx.filter = px > .05 ? `blur(${(px * curScale).toFixed(2)}px)` : "none"; }
function chev(x, y, c, s = 1) {
  ctx.strokeStyle = pal[c]; ctx.lineWidth = 1.6 * s; ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.beginPath(); ctx.moveTo(x, y - 3.2 * s); ctx.lineTo(x + 3.6 * s, y); ctx.lineTo(x, y + 3.2 * s); ctx.stroke();
}
function check(x, y, c, s = 1, w = 1.7) {
  ctx.strokeStyle = typeof c === "string" && pal[c] ? pal[c] : c; ctx.lineWidth = w * s; ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.beginPath(); ctx.moveTo(x - 3.2 * s, y + .2 * s); ctx.lineTo(x - 1 * s, y + 2.6 * s); ctx.lineTo(x + 3.6 * s, y - 2.6 * s); ctx.stroke();
}
function bar(x, y, w, c, a, h) {
  ctx.globalAlpha *= a; ctx.fillStyle = pal[c];
  rr(x, y - h / 2, w, h, h / 2); ctx.fill();
  ctx.globalAlpha /= a;
}
function docIcon(x, y, s, c) {      // x,y = centre
  const w = 8.5 * s, h = 11 * s, f = 3 * s;
  ctx.strokeStyle = c; ctx.lineWidth = 1.3 * s; ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(x - w / 2, y - h / 2); ctx.lineTo(x + w / 2 - f, y - h / 2); ctx.lineTo(x + w / 2, y - h / 2 + f);
  ctx.lineTo(x + w / 2, y + h / 2); ctx.lineTo(x - w / 2, y + h / 2); ctx.closePath(); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - w / 2 + 2 * s, y + .5 * s); ctx.lineTo(x + w / 2 - 2 * s, y + .5 * s);
  ctx.moveTo(x - w / 2 + 2 * s, y + 3 * s); ctx.lineTo(x + w / 2 - 3 * s, y + 3 * s); ctx.stroke();
}
function chipText(txt, x, cy, size, fg, bg, pop = 1, blurPx = 0) {   // left-anchored chip, returns width
  ctx.font = `500 ${size}px ${MONO}`;
  const tw = ctx.measureText(txt).width, ph = size * .55, h = size + 8, w = tw + ph * 2;
  if (pop <= 0) return w;
  ctx.save();
  ctx.translate(x + w / 2, cy); ctx.scale(pop, pop); ctx.translate(-(x + w / 2), -cy);
  blur(blurPx);
  ctx.fillStyle = bg; rr(x, cy - h / 2, w, h, h / 2); ctx.fill();
  ctx.fillStyle = fg; ctx.textBaseline = "middle"; ctx.fillText(txt, x + ph, cy + .5);
  ctx.restore();
  return w;
}

/* message / reply slip: drawn with top-left at (x,y) */
function drawPill(x, y, w, h, o) {
  const S = view.L.sz;
  const kind = o.kind;            // "q" | "reply" | "docked"
  const bg = kind === "q" ? pal.amberSoft : kind === "docked" ? pal.greenSoft : pal.card;
  const edge = kind === "q" ? pal.amber : kind === "docked" ? pal.green : pal.blue;
  if (o.lift > 0) shadow(10 + 18 * o.lift, 3 + 9 * o.lift, .1 + .1 * o.lift);
  ctx.fillStyle = bg; rr(x, y, w, h, S.r * .8); ctx.fill();
  noShadow();
  ctx.globalAlpha *= .55; ctx.strokeStyle = edge; ctx.lineWidth = 1.2; rr(x + .6, y + .6, w - 1.2, h - 1.2, S.r * .8); ctx.stroke(); ctx.globalAlpha /= .55;
  const cy = y + h / 2, gr = h * .29, gx = x + 6 + gr;
  // glyph
  const gpop = o.gpop == null ? 1 : o.gpop;
  ctx.save(); ctx.translate(gx, cy); ctx.scale(gpop, gpop); ctx.translate(-gx, -cy);
  ctx.fillStyle = edge; ctx.beginPath(); ctx.arc(gx, cy, gr, 0, Math.PI * 2); ctx.fill();
  if (kind === "q") {
    ctx.fillStyle = pal.onAccent; ctx.font = `700 ${gr * 1.45}px ${MONO}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("?", gx, cy + gr * .07); ctx.textAlign = "left";
  } else if (kind === "docked") {
    check(gx, cy, pal.onAccent, gr / 6.2, 2);
  } else {
    // return arrow
    const s = gr / 6.5;
    ctx.strokeStyle = pal.onAccent; ctx.lineWidth = 1.7 * s; ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath(); ctx.moveTo(gx + 3.4 * s, gy(cy, 3.2 * s)); ctx.lineTo(gx + 3.4 * s, cy - .6 * s);
    ctx.quadraticCurveTo(gx + 3.4 * s, cy - 2.6 * s, gx + 1.2 * s, cy - 2.6 * s); ctx.lineTo(gx - 3.6 * s, cy - 2.6 * s); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(gx - 1.2 * s, cy - 5 * s); ctx.lineTo(gx - 3.6 * s, cy - 2.6 * s); ctx.lineTo(gx - 1.2 * s, cy - .2 * s); ctx.stroke();
  }
  ctx.restore();
  let bx = gx + gr + 7;
  if (kind === "q" && o.chip !== 0) {
    const p = o.chip == null ? 1 : o.chip;
    const cw = chipText("@jon", bx, cy, S.chip, pal.blue, pal.blueSoft, p, (1 - Math.min(1, p)) * 5);
    bx += cw + 7;
  }
  const bh = S.barH;
  const room = x + w - 9 - bx;
  if (room > 8) {
    const w1 = Math.min(room, room * (kind === "q" ? .55 : .5));
    bar(bx, cy, w1, "ink", .3, bh);
    if (room - w1 - 5 > 8) bar(bx + w1 + 5, cy, (room - w1 - 5) * .6, "ink", .18, bh);
  }
}
const gy = (cy, d) => cy + d;

/* file chip; x,y top-left. k = 0 (icon only) .. 1 (full name) */
function fileChipWidth(k = 1) {
  const S = view.L.sz;
  ctx.font = `500 ${S.chip}px ${MONO}`;
  const full = 30 + ctx.measureText("orders-contract.md").width + 10;
  return lerp(28, full, k);
}
function drawFileChip(x, y, o = {}) {
  const S = view.L.sz, h = S.fileH, k = o.k == null ? 1 : o.k;
  const w = fileChipWidth(k);
  if (o.lift > 0) shadow(8 + 16 * o.lift, 2 + 8 * o.lift, .1 + .08 * o.lift);
  ctx.fillStyle = pal.card; rr(x, y, w, h, 7); ctx.fill(); noShadow();
  ctx.strokeStyle = o.setup ? pal.muted : (o.edge || pal.rule); ctx.lineWidth = 1.1;
  if (o.setup) ctx.setLineDash([2.5, 2.5]);
  rr(x + .55, y + .55, w - 1.1, h - 1.1, 7); ctx.stroke(); ctx.setLineDash([]);
  docIcon(x + 14, y + h / 2, h / 24, o.setup ? pal.muted : pal.blue);
  if (k > .02) {
    ctx.save(); rr(x, y, w, h, 7); ctx.clip();
    ctx.globalAlpha *= clamp((k - .72) / .28, 0, 1);
    ctx.font = `500 ${S.chip}px ${MONO}`; ctx.fillStyle = pal.ink; ctx.textBaseline = "middle";
    ctx.fillText("orders-contract.md", x + 26, y + h / 2 + .5);
    ctx.restore();
  }
  return w;
}

/* small grey sub-slip used inside Jon's own setup */
function drawSub(x, y, w, h, lift = 0) {
  const S = view.L.sz;
  if (lift > 0) shadow(6 + 10 * lift, 2 + 5 * lift, .1);
  ctx.fillStyle = pal.chrome; rr(x, y, w, h, 6); ctx.fill(); noShadow();
  ctx.strokeStyle = pal.muted; ctx.globalAlpha *= .5; ctx.lineWidth = 1; ctx.setLineDash([2.5, 2.5]);
  rr(x + .5, y + .5, w - 1, h - 1, 6); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha /= .5;
  chev(x + 8, y + h / 2, "muted", .85);
  bar(x + 17, y + h / 2, (w - 26) * .62, "muted", .55, S.barH * .9);
}

/* ---------- scene pieces ---------- */
function drawTitle(t) {
  if (t > 4.2 && !STILL) return;
  if (STILL) return;
  const L = view.L, S = L.sz;
  let i = 0;
  const starts = L.desk ? [.45, .58, .71, 1.05, 1.18, 1.31] : [.45, .58, .74, 1.05, 1.18, 1.31];
  ctx.textBaseline = "alphabetic";
  for (const line of L.title.lines) {
    ctx.font = `${line.it ? "italic " : ""}300 ${S.title}px ${SERIF}`;
    let x = L.title.x;
    const space = ctx.measureText(" ").width;
    for (const w of line.words) {
      const t0 = starts[i], ex = 3.12 + i * .035;
      const pin = E.outExpo(seg(t, t0, t0 + .75)), pout = E.inQuad(seg(t, ex, ex + .34));
      const a = pin * (1 - pout);
      if (a > .002) {
        ctx.save();
        ctx.globalAlpha = a;
        blur(16 * (1 - pin) + 12 * pout);
        ctx.fillStyle = line.it ? pal.blue : pal.ink;
        ctx.fillText(w, x, line.y + 22 * (1 - pin) - 18 * pout);
        ctx.restore();
      }
      x += ctx.measureText(w).width + space;
      i++;
    }
  }
}

function drawNames(t) {
  const L = view.L, S = L.sz;
  ctx.textBaseline = "alphabetic";
  for (const [key, t0] of [["maya", 3.85], ["jon", 4.0]]) {
    const n = L.names[key];
    const p = STILL ? 1 : E.outExpo(seg(t, t0, t0 + .8));
    if (p <= 0) continue;
    ctx.save();
    ctx.globalAlpha = p; blur(10 * (1 - p));
    ctx.font = `300 ${S.name}px ${SERIF}`; ctx.fillStyle = pal.ink;
    ctx.fillText(key === "maya" ? "Maya" : "Jon", n.x, n.y + 12 * (1 - p));
    ctx.restore();
  }
}

function drawEnclosure(t) {
  const L = view.L, S = L.sz, e = L.encl;
  const p = grow(t, 13.6, 14.4, E.outCubic);
  if (p <= 0) return;
  const k = 1 - p;
  ctx.save();
  ctx.globalAlpha = p;
  const pad = 10 * k;
  ctx.fillStyle = pal.chrome; ctx.globalAlpha = p * (pal === PAL.dark ? .35 : .45);
  rr(e.x - pad, e.y - pad, e.w + pad * 2, e.h + pad * 2, e.r); ctx.fill();
  ctx.globalAlpha = p * .7;
  ctx.strokeStyle = pal.muted; ctx.lineWidth = 1.2; ctx.setLineDash([5, 5]);
  rr(e.x - pad, e.y - pad, e.w + pad * 2, e.h + pad * 2, e.r); ctx.stroke(); ctx.setLineDash([]);
  ctx.restore();
  // label
  const q = STILL ? 1 : E.outExpo(seg(t, 14.0, 14.75));
  if (q > 0) {
    ctx.save();
    ctx.globalAlpha = q; blur(8 * (1 - q));
    ctx.font = `500 ${S.label}px ${MONO}`; ctx.fillStyle = pal.muted; ctx.textBaseline = "alphabetic";
    try { ctx.letterSpacing = "0.14em"; } catch (_) {}
    ctx.fillText("JON’S SETUP", L.setup.x + 8 * (1 - q), L.setup.y);
    try { ctx.letterSpacing = "0px"; } catch (_) {}
    ctx.restore();
  }
}

function drawLogo(t, F) {
  const L = view.L, g = L.logo;
  const p = STILL ? 1 : spring(t - 4.35, .48, 13);
  if (p <= 0.001) return;
  const pass = F.logoPass;      // squash from slips passing through
  const sx = p * (1 + .14 * pass), sy = p * (1 - .1 * pass);
  const fl = STILL ? 0 : Math.sin((t - 4.35) * Math.PI * 2 / 4.6) * 2.2 * seg(t, 5, 6);
  // rings
  for (const r of F.rings) {
    ctx.save(); ctx.globalAlpha = (1 - r) * .5; ctx.strokeStyle = pal.blue; ctx.lineWidth = 1.5;
    rr(g.x - g.s / 2 - r * 26, g.y + fl - g.s / 2 - r * 26, g.s + r * 52, g.s + r * 52, 15 / 64 * g.s + r * 20); ctx.stroke(); ctx.restore();
  }
  ctx.save();
  ctx.translate(g.x, g.y + fl); ctx.scale(sx, sy);
  const k = g.s / 64; ctx.scale(k, k); ctx.translate(-32, -32);
  shadow(14 / k, 6 / k, .16);
  ctx.fillStyle = pal.logo; rr(0, 0, 64, 64, 15); ctx.fill(); noShadow();
  ctx.fillStyle = "#fff"; ctx.fill(LOGO_BUBBLE);
  ctx.strokeStyle = pal.logo; ctx.lineWidth = 4.5; ctx.lineCap = "round";
  ctx.beginPath(); ctx.moveTo(23, 28.5); ctx.lineTo(41, 28.5); ctx.stroke();
  ctx.fillStyle = pal.logo;
  ctx.beginPath(); ctx.arc(21.5, 28.5, 5.5, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.arc(42.5, 28.5, 5.5, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}
const LOGO_BUBBLE = new Path2D("M17 12h30a7 7 0 0 1 7 7v19a7 7 0 0 1-7 7H29l-10 8v-8h-2a7 7 0 0 1-7-7V19a7 7 0 0 1 7-7z");

/* ---------- terminals ---------- */
function drawTerm(m, t, F) {
  const L = view.L, S = L.sz;
  const e = entry(m, t);
  if (e.a <= 0) return;
  ctx.save();
  ctx.globalAlpha = e.a;
  const cx = m.x + m.w / 2, cy = m.y + m.h / 2;
  ctx.translate(cx, cy + e.dy); ctx.scale(e.sc, e.sc); ctx.translate(-cx, -cy);
  // window
  shadow(30, 12, .1);
  ctx.fillStyle = pal.card; rr(m.x, m.y, m.w, m.h, S.r); ctx.fill(); noShadow();
  ctx.save(); rr(m.x, m.y, m.w, m.h, S.r); ctx.clip();
  ctx.fillStyle = pal.chrome; ctx.fillRect(m.x, m.y, m.w, S.tb);
  ctx.fillStyle = pal.rule; ctx.fillRect(m.x, m.y + S.tb - 1, m.w, 1);
  ctx.restore();
  ctx.strokeStyle = pal.rule; ctx.lineWidth = 1; rr(m.x + .5, m.y + .5, m.w - 1, m.h - 1, S.r); ctx.stroke();
  // window dots
  const dr = L.desk ? 3.6 : 3;
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = pal.rule; ctx.beginPath(); ctx.arc(m.x + 13 + i * (dr * 3.2), m.y + S.tb / 2, dr, 0, Math.PI * 2); ctx.fill();
  }
  // a tab-like hint in the title bar, in the agent's own colour
  ctx.globalAlpha = e.a * .75;
  let tab = m.accent, tabA = .5;
  if (m.id === "J2" && !STILL && t >= HS.land && t < HB.lift + .2) tabA = .95;
  if (m.id === "A" && !STILL && t >= 9.55 && t < 21.2) { tab = t < TR.land ? "amber" : "green"; tabA = t < TR.land ? .85 : lerp(.95, .5, seg(t, 20.6, 21.2)); }
  bar(m.x + 13 + 3 * dr * 3.2 + 6, m.y + S.tb / 2, Math.min(m.w * .22, 46), tab, tabA, S.barH * .9);
  ctx.globalAlpha = e.a;
  if (m.id === "J1") drawInboxChip(m, t);
  // body
  ctx.save();
  ctx.beginPath(); ctx.rect(m.x + 1, m.y + S.tb, m.w - 2, m.h - S.tb - 1); ctx.clip();
  const { items, scroll } = termLayout(m, t);
  for (const it of items) {
    const ln = it.ln, y = m.by + it.y - scroll;
    if (y > m.by + m.bh + 4 || y + ln.h < m.by - 40) continue;
    drawLine(m, ln, y, t, F);
  }
  ctx.restore();
  ctx.restore();
}

function drawLine(m, ln, y, t, F) {
  const S = view.L.sz;
  const a = STILL ? 1 : seg(t, ln.t, ln.t + .07);
  const cy = y + ln.h / 2;
  if (ln.kind === "cmd" || ln.kind === "out" || ln.kind === "ok" || ln.kind === "hand") {
    if (a <= 0) return;
    ctx.save(); ctx.globalAlpha *= a;
    for (const sgm of ln.segs) {
      if (sgm.g === "chev") chev(m.bx + sgm.x + 1, cy, sgm.c);
      else if (sgm.g === "check") check(m.bx + sgm.x + 3, cy, sgm.c, .95);
      else bar(m.bx + sgm.x, cy, sgm.w, sgm.c, sgm.a, S.barH);
    }
    if (ln.kind === "hand") {
      // hands work to another agent: a small grey arrow + slot outline
      const x0 = m.bx + m.bw * .3;
      ctx.strokeStyle = pal.muted; ctx.lineWidth = 1.3; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(x0, cy); ctx.lineTo(x0 + 12, cy); ctx.moveTo(x0 + 8, cy - 3.5); ctx.lineTo(x0 + 12, cy); ctx.lineTo(x0 + 8, cy + 3.5); ctx.stroke();
    }
    ctx.restore();
    return;
  }
  const L = view.L;
  if (ln.kind === "q") return drawQuestionLine(m, ln, y, t, F);
  if (ln.kind === "in") {
    if (t < ln.show && !STILL) return;
    drawPill(m.bx, y + S.vg, m.pillW, S.pillH, { kind: "q", chip: 1, lift: 0 });
    return;
  }
  if (ln.kind === "file") {
    if (t < ln.show && !STILL) return;
    drawFileChip(m.bx, y + S.vg, { edge: m.id === "A" ? pal.green : null });
    return;
  }
  if (ln.kind === "reply") {
    if (a <= 0) return;
    if (STILL || t >= TR.lift) return;          // lifted out: the flying slip is the only copy
    const press = E.inQuad(seg(t, TR.lift - .18, TR.lift));
    ctx.save();
    ctx.globalAlpha *= a;
    const px = m.bx, py = y + S.vg + press * 2;
    drawPill(px, py, m.pillW, S.pillH, { kind: "reply", lift: 0 });
    if (t >= ln.chipAt) drawFileChip(px, py + S.pillH + 2 * S.vg, { setup: true });
    ctx.restore();
    return;
  }
  if (ln.kind === "sub") {
    if (t < ln.show && !STILL) return;
    drawSub(m.bx, y + 2, m.pillW, ln.h - 4, 0);
    return;
  }
  if (ln.kind === "fileout") {
    if (a <= 0) return;
    ctx.save(); ctx.globalAlpha *= a;
    docIcon(m.bx + 6, cy, .9, pal.muted);
    bar(m.bx + 16, cy, m.bw * .45, "muted", .55, S.barH);
    ctx.restore();
  }
}

function drawQuestionLine(m, ln, y, t, F) {
  const S = view.L.sz;
  const px = m.bx, py = y + S.vg, w = m.pillW, h = S.pillH;
  if (!STILL && t < 9.55) {
    const a = seg(t, ln.t, ln.t + .08);
    const press = E.inQuad(seg(t, 9.3, 9.55));
    const chip = t < 8.9 ? 0 : spring(t - 8.9, .5, 17);
    ctx.save(); ctx.globalAlpha *= a;
    const cx = px + w / 2, cy = py + h / 2;
    ctx.translate(cx, cy + press * 2.5); ctx.scale(1 - .03 * press, 1 - .05 * press); ctx.translate(-cx, -cy);
    drawPill(px, py, w, h, { kind: "q", chip, lift: 0 });
    ctx.restore();
    return;
  }
  // the slot left behind
  const docked = STILL || t >= 20.25;
  const outlineIn = STILL ? 1 : E.outCubic(seg(t, 9.62, 10.0));
  const dock = STILL ? 1 : E.outCubic(seg(t, 19.9, 20.15));
  ctx.save();
  if (!docked) {
    ctx.globalAlpha *= outlineIn;
    ctx.fillStyle = pal.amberSoft; ctx.globalAlpha *= .55; rr(px, py, w, h, S.r * .8); ctx.fill(); ctx.globalAlpha /= .55;
    ctx.strokeStyle = dock > 0 ? pal.green : pal.amber; ctx.lineWidth = 1.4;
    ctx.setLineDash(dock > .5 ? [] : [4, 3.5]);
    rr(px + .7, py + .7, w - 1.4, h - 1.4, S.r * .8); ctx.stroke(); ctx.setLineDash([]);
  } else {
    const k = STILL ? 1 : seg(t, 20.25, 20.3);
    ctx.globalAlpha *= k;
    drawPill(px, py, w, h, { kind: "docked", lift: 0 });
  }
  ctx.restore();
}

function drawInboxChip(m, t) {
  const S = view.L.sz;
  const p = STILL ? 1 : spring(t - 4.8, .5, 16);
  if (p <= .001) return;
  const nod = STILL ? 0 : wobble(t - 13.0, 6, 16) * (t > 13 ? 1 : 0);
  const size = 12;
  ctx.font = `500 ${size}px ${MONO}`;
  const tw = ctx.measureText("inbox").width, w = tw + 14, h = S.tb - 7;
  const x = m.x + m.w - w - 7, cy = m.y + S.tb / 2;
  ctx.save();
  const sc = p * (1 + .12 * nod);
  ctx.translate(x + w / 2, cy); ctx.scale(sc, sc); ctx.translate(-(x + w / 2), -cy);
  blur(4 * (1 - Math.min(1, p)));
  ctx.fillStyle = pal.blueSoft; rr(x, cy - h / 2, w, h, h / 2); ctx.fill();
  ctx.strokeStyle = pal.blue; ctx.globalAlpha *= .35; ctx.lineWidth = 1; rr(x + .5, cy - h / 2 + .5, w - 1, h - 1, h / 2); ctx.stroke(); ctx.globalAlpha /= .35;
  ctx.fillStyle = pal.blue; ctx.textBaseline = "middle"; ctx.fillText("inbox", x + 7, cy + .5);
  ctx.restore();
}

/* ---------- flights ---------- */
// lift -> leg1 (to RainCLI) -> inside the tile -> leg2 (to a hover point over the slot) -> pause -> drop -> land
const TM = {   // message out
  lift: 9.55, leg1a: 10.6, leg1b: 11.7, leg2a: 11.88, hov: 12.58, drop: 12.72, land: 12.92, hand: 13.3 };
const TR = {   // reply back
  lift: 17.5, leg1a: 17.8, leg1b: 18.6, leg2a: 18.76, hov: 19.42, drop: 19.68, land: 19.9, hand: 20.25 };
// inside Jon's setup (his own agents, not RainCLI): pop -> travel -> pause over the slot -> drop -> land
const HS = {   // the work, from Jon's inbox agent to his other agent
  pop: 14.2, go: 14.4, hov: 15.02, drop: 15.12, land: 15.3, hand: 15.45 };
const HB = {   // the result, back to the inbox agent
  lift: 16.05, go: 16.2, hov: 16.78, drop: 16.86, land: 17.0, hand: 17.15 };

function slipFlight(t, T_, F, type, o) {
  const L = view.L, l1 = o.pre.len;
  const lift = L.desk ? 24 : 15;
  // aim at where the slot will be when the slip lands (the inbox's space is still opening on the way in)
  const slot = o.slot(Math.max(t, T_.land));
  const hov = { x: slot.cx, y: slot.cy - o.inDir * lift };
  const p2 = makePath([o.r2, vbez(o.r2.p1, hov, o.inDir * 40, o.inDir * 40)]);
  const passT = T_.leg1b - .05;
  F.logoPass += t > passT ? wobble(t - passT, 6.5, 17) * .9 : 0;
  if (t > passT && t < passT + .9) F.rings.push(E.outCubic(seg(t, passT, passT + .9)));
  if (t < T_.lift || t >= T_.hand) return;
  const posAt = tt => {
    if (tt < T_.leg1a) {                                   // lift off the line, springy, then a held hover
      const p = spring(tt - T_.lift, .55, 15);
      const bob = Math.sin((tt - T_.lift) * 7) * 1.2 * seg(tt, T_.lift + .45, T_.lift + .75);
      return { x: lerp(o.start.x, o.hover0.x, p), y: lerp(o.start.y, o.hover0.y, p) + bob, s: 1 + .08 * p, lift: p };
    }
    if (tt < T_.leg1b) { const q = pathAt(o.pre, E.inOutCubic(seg(tt, T_.leg1a, T_.leg1b)) * l1); return { x: q.x, y: q.y, s: 1.08, lift: 1 }; }
    if (tt < T_.leg2a) return { x: L.logo.x, y: L.logo.y, s: 1.08, lift: 1 };
    if (tt < T_.drop) {                                    // out of the tile with momentum, settle over the slot
      const q = pathAt(p2, E.outCubic(seg(tt, T_.leg2a, T_.hov)) * p2.len);
      const bob = tt > T_.hov ? Math.sin((tt - T_.hov) * 10) * 1.1 * (1 - seg(tt, T_.drop - .06, T_.drop)) : 0;
      return { x: q.x, y: q.y + bob, s: 1.08, lift: 1 };
    }
    const k = E.inQuad(seg(tt, T_.drop, T_.land));          // the drop: accelerates into place
    return { x: lerp(hov.x, slot.cx, k), y: lerp(hov.y, slot.cy, k), s: lerp(1.08, 1, k), lift: 1 - k };
  };
  const p = posAt(t), pa = posAt(t - 1 / 60), pb = posAt(t + 1 / 60);
  const vx = (pb.x - pa.x) * 30;
  const vis = t < T_.leg1b - .14 ? 1 : t >= T_.leg2a + .16 ? 1 :
    (t < T_.leg1b ? 1 - seg(t, T_.leg1b - .14, T_.leg1b) : t < T_.leg2a ? 0 : seg(t, T_.leg2a, T_.leg2a + .16));
  const sq = t >= T_.land ? wobble(t - T_.land, 8.5, 17.5) : 0;
  if (vis <= 0) return;
  // near the tile on the way in or out, the slip is drawn beneath it, so it goes *into* RainCLI
  const nearTile = Math.hypot(p.x - L.logo.x, p.y - L.logo.y) < L.logo.s * 2.6;
  F.cards.push({ under: t > T_.leg1a && t < T_.leg2a + .35 && nearTile, type, x: p.x, y: p.y, s: p.s * lerp(.3, 1, vis), a: E.outQuad(vis),
    rot: clamp(vx * .00045, -.09, .09) * (t < T_.hov ? 1 : 0), lift: p.lift, sq, w: o.w, h: o.h,
    dock: type === "reply" && t >= T_.land ? 1 : 0, dockU: t - T_.land });
}

function vbez(a, b, da, db) {        // bezier leaving a vertically (da) and arriving at b vertically (db)
  return bez(a, { x: a.x, y: a.y + da }, { x: b.x, y: b.y - db }, b);
}

let flightCache = null;
function flightGeometry() {
  const L = view.L;
  if (flightCache && flightCache.L === L) return flightCache;
  const R = L.routes, A = L.terms.A, J1 = L.terms.J1;
  const g = { L };
  const S = L.sz;
  const lift = L.desk ? 30 : 18;
  // message: from the question line, lifted
  const q = pillRect(A, "q", TM.lift);
  g.mHover = { x: q.cx, y: q.cy + R.vA * lift };
  g.mPre = makePath([vbez(g.mHover, R.out1.p0, R.vA * 40, R.vA * 40), R.out1]);
  // reply: from Jon's inbox reply line
  const rp = pillRect(J1, "reply", TR.lift);
  g.rStart = { x: rp.cx, y: rp.cy };
  g.rHover = { x: rp.cx, y: rp.cy - 5 };     // lifts toward the camera, not up over the file line above it
  g.rPre = makePath([vbez(g.rHover, R.ret1.p0, -40, -40), R.ret1]);
  g.qStart = { x: q.cx, y: q.cy };
  flightCache = g;
  return g;
}

function computeFlights(t) {
  const L = view.L, R = L.routes, S = L.sz, A = L.terms.A, J1 = L.terms.J1, J2 = L.terms.J2;
  const g = flightGeometry();
  const F = { cards: [], logoPass: 0, rings: [], softRings: [] };
  if (STILL) return F;
  // ---- message out
  slipFlight(t, TM, F, "q", {
    start: g.qStart, hover0: g.mHover, pre: g.mPre, r2: R.out2,
    slot: tt => pillRect(J1, "in", tt), inDir: 1, w: A.pillW, h: S.pillH });
  // ---- inside Jon's setup: hand-off to another of his agents and back (grey, dotted, never via RainCLI)
  {
    const dir = Math.sign(J2.x - J1.x), lift = L.desk ? 12 : 9;
    // through the gap between the two windows
    const across = () => {
      const exitX = dir < 0 ? J1.x - 2 : J1.x + J1.w + 2, entX = dir < 0 ? J2.x + J2.w + 2 : J2.x - 2;
      return { exitX, entX };
    };
    // the work: from the hand-off line in Jon's inbox agent to a slot in his other agent
    if (t >= HS.pop - .05) {
      const handLn = J1.lines.find(l => l.id === "hand"), subLn = J2.lines.find(l => l.id === "sub");
      const from = { x: J1.bx + J1.bw * .3 + 18, y: lineTop(J1, "hand", HS.pop) + handLn.h / 2 };
      const subH = subLn.h - 4, slot = { x: J2.bx + J2.pillW / 2, y: lineTop(J2, "sub", HS.land) + 2 + subH / 2 };
      const hov = { x: slot.x, y: slot.y - lift };
      const { exitX, entX } = across();
      const ex = { x: exitX, y: from.y - 18 }, en = { x: entX, y: hov.y };
      const P = makePath([bez(from, { x: from.x, y: from.y - 30 }, { x: ex.x - dir * 40, y: ex.y }, ex),
        bez(ex, { x: ex.x + dir * 24, y: ex.y }, { x: en.x - dir * 24, y: en.y }, en),
        bez(en, { x: en.x + dir * 16, y: en.y }, { x: hov.x - dir * 30, y: hov.y }, hov)]);
      if (t < HS.hand) {
        // carried, not drawn: lifted toward the camera in transit, tilting with its velocity, settling as it drops
        const posAt = tt => {
          if (tt < HS.hov) { const q = pathAt(P, E.inOutCubic(seg(tt, HS.go, HS.hov)) * P.len); return { x: q.x, y: q.y, up: seg(tt, HS.pop, HS.go + .15), lf: 1 }; }
          if (tt < HS.drop) return { x: hov.x, y: hov.y + Math.sin((tt - HS.hov) * 12) * .8, up: 1, lf: 1 };
          const d = E.inQuad(seg(tt, HS.drop, HS.land)); return { x: hov.x, y: lerp(hov.y, slot.y, d), up: 1 - d, lf: 1 - d };
        };
        const q = posAt(t), vx = (posAt(t + 1 / 60).x - posAt(t - 1 / 60).x) * 30;
        const pop = spring(t - HS.pop, .6, 16);
        const sq = t >= HS.land ? wobble(t - HS.land, 9, 18) : 0;
        F.cards.push({ type: "sub", x: q.x, y: q.y, s: lerp(.4, 1, pop) * (1 + .12 * E.outCubic(q.up)), a: Math.min(1, pop * 1.5), w: J2.pillW, h: subH,
          lift: q.lf, rot: clamp(vx * .00045, -.09, .09) * (t < HS.hov ? 1 : 0), sq });
      }
      if (t > HS.land && t < HS.land + .7) F.softRings.push({ p: E.outCubic(seg(t, HS.land, HS.land + .7)), x: J2.bx, y: slot.y - subH / 2, w: J2.pillW, h: subH });
    }
    // the result: the file goes back and lands on the inbox agent's reply
    if (t >= HB.lift - .05) {
      const foLn = J2.lines.find(l => l.id === "fo");
      const from = { x: J2.bx + 6, y: lineTop(J2, "fo", HB.lift) + foLn.h / 2 };
      const chipTop = lineTop(J1, "reply", HB.land) + S.vg + S.pillH + 2 * S.vg;
      const slot = { x: J1.bx + 14, y: chipTop + S.fileH / 2 };      // follows the file icon; the card grows rightward from it
      const hov = { x: slot.x, y: slot.y - lift };
      const { exitX, entX } = across();
      const out = { x: entX, y: from.y + 14 }, inn = { x: exitX, y: hov.y };
      const P = makePath([bez(from, { x: from.x + (-dir) * 40, y: from.y + 26 }, { x: out.x + dir * 30, y: out.y }, out),
        bez(out, { x: out.x - dir * 24, y: out.y }, { x: inn.x + dir * 24, y: inn.y }, inn),
        bez(inn, { x: inn.x - dir * 16, y: inn.y }, { x: hov.x + dir * 40, y: hov.y }, hov)]);
      if (t < HB.hand) {
        const grow1 = E.outCubic(seg(t, HB.lift, HB.lift + .32));
        const posAt = tt => {
          if (tt < HB.hov) { const q = pathAt(P, E.inOutSine(seg(tt, HB.go, HB.hov)) * P.len); return { x: q.x, y: q.y, lf: 1 }; }
          if (tt < HB.drop) return { x: hov.x, y: hov.y + Math.sin((tt - HB.hov) * 12) * .8, lf: 1 };
          const d = E.inQuad(seg(tt, HB.drop, HB.land)); return { x: hov.x, y: lerp(hov.y, slot.y, d), lf: 1 - d };
        };
        const q = posAt(t), vx = (posAt(t + 1 / 60).x - posAt(t - 1 / 60).x) * 30;
        const sq = t >= HB.land ? wobble(t - HB.land, 9, 18) * .7 : 0;
        F.cards.push({ type: "file", setup: true, x: q.x, y: q.y, k: grow1, s: lerp(1, 1.08, Math.sin(Math.PI * seg(t, HB.lift, HB.land))), a: 1, lift: q.lf,
          rot: clamp(vx * .00045, -.09, .09) * (t < HB.hov ? 1 : 0), sq });
      }
      if (t > HB.land && t < HB.land + .7) F.softRings.push({ p: E.outCubic(seg(t, HB.land, HB.land + .7)), x: J1.bx, y: chipTop, w: fileChipWidth(1), h: S.fileH });
    }
  }
  // ---- reply back
  slipFlight(t, TR, F, "reply", {
    start: g.rStart, hover0: g.rHover, pre: g.rPre, r2: R.ret2,
    slot: tt => pillRect(A, "q", tt), inDir: -R.vA, w: A.pillW, h: S.pillH });
  // the snap: one ring from the slot
  if (t > TR.land && t < TR.land + .8) F.dockRing = { p: E.outCubic(seg(t, TR.land, TR.land + .8)), r: pillRect(A, "q", t) };
  return F;
}

function drawLandingRings(F) {
  for (const r of F.softRings) {
    const g = r.p * 9;
    ctx.save(); ctx.globalAlpha = (1 - r.p) * .7; ctx.strokeStyle = pal.muted; ctx.lineWidth = 1.4;
    rr(r.x - g, r.y - g, r.w + 2 * g, r.h + 2 * g, 7 + g); ctx.stroke(); ctx.restore();
  }
}

function drawCards(F, under) {
  const S = view.L.sz;
  for (const c of F.cards) {
    if (!!c.under !== under) continue;
    ctx.save();
    ctx.globalAlpha = c.a;
    ctx.translate(c.x, c.y); ctx.rotate(c.rot || 0);
    const sx = c.s * (1 + .06 * (c.sq || 0)), sy = c.s * (1 - .09 * (c.sq || 0));
    ctx.scale(sx, sy);
    if (c.type === "q") {
      drawPill(-c.w / 2, -c.h / 2, c.w, c.h, { kind: "q", chip: 1, lift: c.lift });
    } else if (c.type === "reply") {
      // file hangs under the reply, joined by a little clip
      ctx.strokeStyle = pal.blue; ctx.globalAlpha *= .6; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.moveTo(-c.w / 2 + 14, c.h / 2 - 2); ctx.lineTo(-c.w / 2 + 14, c.h / 2 + 2 * S.vg + 3); ctx.stroke();
      ctx.globalAlpha /= .6;
      drawFileChip(-c.w / 2, -c.h / 2 + c.h + 2 * S.vg, { lift: c.lift * .6, edge: c.dock > 0 ? pal.green : null });
      drawPill(-c.w / 2, -c.h / 2, c.w, c.h, { kind: c.dock ? "docked" : "reply", lift: c.lift, gpop: c.dock ? lerp(.4, 1, spring(c.dockU, .42, 20)) : 1 });
    } else if (c.type === "sub") {
      drawSub(-c.w / 2, -c.h / 2, c.w, c.h, c.lift);
    } else if (c.type === "file") {
      drawFileChip(-14, -S.fileH / 2, { k: c.k, lift: c.lift, setup: c.setup });
    }
    ctx.restore();
  }
  if (F.dockRing && !under) {
    const { p, r } = F.dockRing;
    ctx.save(); ctx.globalAlpha = (1 - p) * .7; ctx.strokeStyle = pal.green; ctx.lineWidth = 1.6;
    const g = p * 14;
    rr(r.x - g, r.y - g, r.w + 2 * g, r.h + 2 * g, S.r * .8 + g); ctx.stroke(); ctx.restore();
  }
}

/* ---------- frame ---------- */
function render(t, still = false) {
  STILL = still;
  const { W, H, dpr, s, L } = view;
  if (!L) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.filter = "none"; ctx.globalAlpha = 1;
  ctx.fillStyle = pal.bg; ctx.fillRect(0, 0, W, H);
  const cam = still ? { cx: L.D.w / 2, cy: L.D.h / 2, z: 1 } : camAt(t);
  const z = cam.z * s;
  ctx.setTransform(dpr * z, 0, 0, dpr * z, dpr * (W / 2 - cam.cx * z), dpr * (H / 2 - cam.cy * z));
  curScale = dpr * z;
  const F = computeFlights(t);
  drawTitle(t);
  drawEnclosure(t);
  for (const id of ["M2", "M3", "J2", "J3", "A", "J1"]) drawTerm(L.terms[id], t, F);
  drawNames(t);
  drawCards(F, true);
  drawLogo(t, F);
  drawLandingRings(F);
  drawCards(F, false);
  noShadow(); ctx.filter = "none";
  const f = still ? 0 : E.inOutSine(seg(t, 23.3, 24));
  if (f > 0) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = f; ctx.fillStyle = pal.bg; ctx.fillRect(0, 0, W, H); ctx.globalAlpha = 1;
  }
}

/* ---------- sizing ---------- */
function resize() {
  const W = Math.round(root.clientWidth) || 1;
  const mode = W >= 960 ? "desk" : "phone";
  let H, s;
  if (mode === "desk") {
    H = Math.round(clamp(W * 720 / 1180, 520, 860));
    s = Math.min(W / 1180, H / 720);
  } else {
    s = Math.min(W / 390, 1.5);
    H = Math.round(800 * s);
  }
  // The stage's height is the same formula in demo-teams.css (no inline style).
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  }
  if (view.mode !== mode || !view.L) { view.L = buildLayout(mode); flightCache = null; }
  Object.assign(view, { W, H, dpr, s, mode });
  draw();
}

/* ---------- playback ---------- */
const mqReduce = matchMedia("(prefers-reduced-motion: reduce)");
let clock = 0, last = 0, raf = 0, userPaused = false, onScreen = true, ready = false;
const STILL_T = 22.75;
function reduced() { return mqReduce.matches && HOLD == null; }
function draw() {
  if (!ready) return;
  if (HOLD != null) render(HOLD, mqReduce.matches);
  else if (reduced()) render(STILL_T, true);
  else render(clock % T);
}
function shouldPlay() { return ready && HOLD == null && !reduced() && !userPaused && onScreen && !document.hidden; }
function tick(now) {
  raf = 0;
  if (!shouldPlay()) return;
  const dt = last ? Math.min(.1, (now - last) / 1000) : 0;
  last = now; clock = (clock + dt) % T;
  render(clock);
  raf = requestAnimationFrame(tick);
}
function update() {
  root.classList.toggle("is-still", HOLD == null && reduced());
  if (shouldPlay()) { if (!raf) { last = 0; raf = requestAnimationFrame(tick); } }
  else { if (raf) cancelAnimationFrame(raf); raf = 0; draw(); }
}
// The pause (WCAG 2.2.2): its name stays "Pause animation"; aria-pressed (true = paused) carries the state and the
// icon follows it in the CSS. It is rendered hidden, so a page without this script has no dead control.
function setPaused(p) {
  userPaused = p;
  btn.setAttribute("aria-pressed", p ? "true" : "false");
  update();
}
btn.hidden = false;
btn.addEventListener("click", () => setPaused(!userPaused));
// A touch screen has no hover: a tap on the animation shows the pause, and it stays until a tap elsewhere.
if (matchMedia("(hover: none)").matches) {
  document.addEventListener("click", e => { root.classList.toggle("adm-touch-shown", root.contains(e.target)); });
}
// It starts only when really in view (review S1): 40% of the section, or of the viewport when the section is the
// taller of the two (a landscape phone), so it doesn't start alongside the app demo at the desktop fold.
new IntersectionObserver(es => {
  const e = es[es.length - 1];
  onScreen = e.isIntersecting && (e.intersectionRatio >= .4 || e.intersectionRect.height >= .4 * window.innerHeight);
  update();
}, { threshold: [0, .1, .2, .3, .4, .5, .6, .8, 1] }).observe(root);
document.addEventListener("visibilitychange", update);
mqReduce.addEventListener("change", update);
mqDark.addEventListener("change", () => { pickTheme(); draw(); });
new ResizeObserver(resize).observe(root);
window.addEventListener("resize", resize);

pickTheme();
const fontsReady = Promise.all([
  document.fonts.load(`300 40px ${SERIF}`), document.fonts.load(`italic 300 40px ${SERIF}`),
  document.fonts.load(`500 12px ${MONO}`), document.fonts.load(`700 12px ${MONO}`)
]).catch(() => {});
Promise.race([fontsReady, new Promise(r => setTimeout(r, 1500))]).then(() => {
  ready = true; flightCache = null; resize(); update();
  root.dataset.admReady = "1";  // the section's own ready mark (review N1), not a page-global one
});
})();
