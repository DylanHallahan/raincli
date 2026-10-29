// App mode only (protocol §20, §20.6 M4): behaviour new in v0.5.5, loaded by base_app.html after live.js. Each part
// is its own function and everything degrades to the plain pages without JavaScript. The shared app.js keeps
// the v0.5.4 list code (day groups, the Team open state); this file adds to it and never replaces its hooks.
(function () {
  "use strict";
  var body = document.body;

  // §20.6 M1 (P2): Inbox opens the latest conversation (archived ones excluded; with groups, the newest activity of
  // either kind), when the list is visible: above the app-mode breakpoint. Below it, Inbox shows the list. A toast
  // opens its own thread directly (the handoff's ``next``), so it never passes through here.
  function openLatest() {
    // §20.7: only where the server marks it (an app whose hidden window keeps its page hidden, 0.5.5 or later).
    if (!body.classList.contains("rc-inbox") || !document.querySelector("[data-open-latest]")) return false;
    var list = document.querySelector("section.rc-list"), main = document.querySelector(".rc-main");
    // D12 (§22 G12 L4): the server marks the newest activity, a 1:1 conversation or a group (data-newest).
    var first = list && (list.querySelector(".rc-convs .rc-conv[data-newest]") || list.querySelector(".rc-convs .rc-conv"));
    // Above the breakpoint both show; below it Inbox shows only the list (its main pane is hidden), and stays.
    if (!first || !main || getComputedStyle(list).display === "none" || getComputedStyle(main).display === "none") return false;
    location.replace(first.href);
    return true;
  }

  // §20.3: the composer is ready on a conversation, but only in a focused window, never over a #m-<id> anchor or
  // another element the reader is in, and without scrolling (§20.4 R3).
  function composerReady() {
    var area = document.querySelector(".rc-compose textarea#body");
    if (!area || !document.querySelector("[data-thread]") || !document.hasFocus()) return;
    if (/^#m-/.test(location.hash)) return;
    if (document.activeElement && document.activeElement !== body && document.activeElement !== document.documentElement) return;
    area.focus({preventScroll: true});
  }

  // §20.4 R5: the trail is the toggle for its event history. The server sends a <details> whose summary is the
  // trail; here the summary is a button with aria-expanded, kept in step on every toggle (Enter and Space work
  // natively). Trails that live.js inserts or swaps in are marked as they arrive.
  function trailToggles(root) {
    var found = root.querySelectorAll ? root.querySelectorAll("details.rc-trail-box > summary") : [];
    Array.prototype.forEach.call(found, function (summary) {
      summary.setAttribute("role", "button");
      summary.setAttribute("aria-expanded", summary.parentNode.open ? "true" : "false");
    });
  }
  function watchThread() {
    var thread = document.querySelector("[data-thread]");
    if (!thread || !window.MutationObserver) return;
    new MutationObserver(function (changes) {
      changes.forEach(function (change) {
        Array.prototype.forEach.call(change.addedNodes, function (node) { if (node.nodeType === 1) trailToggles(node); });
      });
    }).observe(thread, {childList: true, subtree: true});
  }

  document.addEventListener("toggle", function (event) {
    var target = event.target;
    if (target.matches && target.matches("details.rc-trail-box")) {
      var summary = target.querySelector(":scope > summary");
      if (summary) summary.setAttribute("aria-expanded", target.open ? "true" : "false");
    }
  }, true);

  // The shell is the window (the "everything at the top" bug). Two guards, both silent no-ops normally:
  // - the height fallback: if <body> doesn't measure innerHeight (a host's stale 100%/100vh), set --rc-app-h, a CSS
  //   custom property through CSSOM (the CSP allows it; templates carry no style attributes), which app-mode.css uses;
  // - the scroll guard: the document never scrolls in app mode (only the panes do); if anything scrolled it, put it
  //   back to 0.
  var shell = {heightFix: false, scrollGuard: false};
  function fitShell() {
    var height = window.innerHeight;
    if (!height || Math.abs(body.getBoundingClientRect().height - height) <= 2) return;
    document.documentElement.style.setProperty("--rc-app-h", height + "px");
    shell.heightFix = true;
  }
  function unscroll() {
    var doc = document.scrollingElement || document.documentElement;
    if (!doc.scrollTop && !doc.scrollLeft) return;
    doc.scrollTop = 0;
    doc.scrollLeft = 0;
    shell.scrollGuard = true;
  }
  function settleShell() { fitShell(); unscroll(); }
  settleShell();
  window.addEventListener("load", settleShell);
  window.addEventListener("resize", settleShell);
  window.addEventListener("pageshow", settleShell);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") settleShell(); });
  document.addEventListener("focusin", unscroll);
  window.addEventListener("scroll", unscroll, {passive: true});
  window.addEventListener("hashchange", unscroll);

  // TEMP layout diagnostic (remove when the shell bug is closed). Checked at most three times per page load (on load,
  // on the first show, on the first resize), after the shell guards ran, and sent ONLY for an anomaly: the document
  // scrolled, a guard fired, unexpected body children, or a pane not reaching the window's bottom. A healthy window
  // sends nothing. Numbers and short class names only: no ids, URLs or text. Failures are ignored.
  (function () {
    var live = body.getAttribute("data-live-url"), csrf = body.getAttribute("data-csrf");
    if (!live || !csrf || !window.fetch) return;
    var url = live.replace(/\/app\/live$/, "/app/layout-diag"), sent = {}, count = 0;
    // Printable ASCII only (anything else becomes "?"), then trimmed: an odd class name can't spoil a report.
    function cut(text, n) { return String(text || "").replace(/[^\x20-\x7e]/g, "?").slice(0, n); }
    function bottom(selector) {  // null when absent or not rendered (the list below the narrow breakpoint)
      var el = document.querySelector(selector), rect = el && el.getBoundingClientRect();
      return rect && rect.height ? Math.round(rect.bottom) : null;
    }
    var EXPECTED = ["nav.rc-nav", "section.rc-list", "main.rc-main"];
    function anomalous(d) {
      var panes = [d.nav_bottom, d.list_bottom, d.main_bottom, d.compose_bottom];
      return d.doc_scroll_top !== 0 || d.height_fix || d.scroll_guard ||
        d.children_n < 2 || d.children_n > 3 || d.children.some(function (c) { return EXPECTED.indexOf(c) < 0; }) ||
        panes.some(function (b) { return b !== null && Math.abs(b - d.inner_h) > 2; });
    }
    function report(event) {
      if (sent[event] || count >= 3) return;
      sent[event] = true;
      count += 1;
      requestAnimationFrame(function () {
        var doc = document.scrollingElement || document.documentElement, nav = document.querySelector(".rc-nav");
        var style = getComputedStyle(body), kids = Array.prototype.slice.call(body.children);
        var data = {
          event: event,
          page: body.classList.contains("rc-inbox") ? "inbox" : (document.querySelector("[data-thread]") ? "conversation" : "other"),
          inner_w: window.innerWidth, inner_h: window.innerHeight, client_h: document.documentElement.clientHeight,
          dpr: Math.round(window.devicePixelRatio * 100) / 100,
          children_n: kids.length,
          children: kids.slice(0, 10).map(function (el) {
            return cut(el.tagName.toLowerCase() + (el.className && typeof el.className === "string" ?
              "." + el.className.trim().split(/\s+/).join(".") : ""), 40);
          }),
          grid_rows: cut(style.gridTemplateRows, 80), body_height: cut(style.height, 40),
          nav_bottom: bottom(".rc-nav"), list_bottom: bottom("section.rc-list"), main_bottom: bottom("main.rc-main"),
          compose_bottom: bottom(".rc-compose"),
          doc_scroll_top: Math.round(doc.scrollTop), doc_scroll_h: doc.scrollHeight, doc_client_h: doc.clientHeight,
          nav_scroll_h: nav ? nav.scrollHeight : null, nav_client_h: nav ? nav.clientHeight : null,
          visibility: cut(document.visibilityState, 16), height_fix: shell.heightFix, scroll_guard: shell.scrollGuard
        };
        if (!anomalous(data)) return;
        fetch(url, {method: "POST", credentials: "same-origin", keepalive: true,
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({csrf_token: csrf, layout: data})}).catch(function () {});
      });
    }
    if (document.readyState === "complete") { report("load"); } else { window.addEventListener("load", function () { report("load"); }); }
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") report("show"); });
    window.addEventListener("resize", function () { report("resize"); });
  }());

  if (openLatest()) return;
  trailToggles(document);
  watchThread();
  // Focus may arrive after this script runs (early in a navigation, or a window shown later): try again then.
  composerReady();
  window.addEventListener("load", composerReady);
  window.addEventListener("focus", composerReady, {once: true});
}());
