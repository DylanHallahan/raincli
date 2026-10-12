// App mode only (protocol §20, §20.6 M4): behaviour new in v0.5.5, loaded by base_app.html after live.js. Each part
// is its own function and everything degrades to the plain pages without JavaScript. The shared app.js keeps
// the v0.5.4 list code (day groups, the Team open state); this file adds to it and never replaces its hooks.
(function () {
  "use strict";
  var body = document.body;
  var TEAM_KEY = "rc-team-open:";  // app.js's key for the Team section's open state, per viewer
  // R7 (short windows: Team collapsed below 600 px tall, the list fade) is frozen for v0.5.5. Its code stays dormant
  // behind this switch; true restores it (with the .rc-convs.rc-more rule in app-mode.css).
  var R7 = false;

  // §20.6 M1 (P2): Inbox opens the latest conversation, the first in the list (ordered by last seq, archived ones
  // excluded), when the list is visible: above the app-mode breakpoint. Below it, Inbox shows the list. A toast
  // opens its own thread directly (the handoff's ``next``), so it never passes through here.
  function openLatest() {
    // §20.7: only where the server marks it (an app whose hidden window keeps its page hidden, 0.5.5 or later).
    if (!body.classList.contains("rc-inbox") || !document.querySelector("[data-open-latest]")) return false;
    var list = document.querySelector("section.rc-list"), main = document.querySelector(".rc-main");
    var first = list && list.querySelector(".rc-convs .rc-conv");
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

  // §20.4 R7: below 600 px tall the Team section starts collapsed, unless the viewer chose otherwise (a saved
  // choice wins). The automatic close is not saved as a choice: its toggle event is stopped before app.js
  // would store it.
  var autoClosing = null;
  function shortWindowTeam() {
    if (window.innerHeight >= 600) return;
    var details = document.querySelector("details[data-team]");
    if (!details || !details.open) return;
    var saved = null;
    try { saved = localStorage.getItem(TEAM_KEY + details.getAttribute("data-team")); } catch (e) { saved = null; }
    if (saved !== null) return;
    autoClosing = details;
    details.open = false;
  }

  // §20.4 R7: an overflowing conversation list fades at its bottom edge while there is more below.
  function listFade() {
    var list = document.querySelector(".rc-list .rc-convs");
    if (!list) return;
    var update = function () { list.classList.toggle("rc-more", list.scrollHeight - list.scrollTop - list.clientHeight > 4); };
    if (!list.hasAttribute("data-fade")) {
      list.setAttribute("data-fade", "");
      list.addEventListener("scroll", update, {passive: true});
    }
    update();
  }

  document.addEventListener("toggle", function (event) {
    var target = event.target;
    if (target === autoClosing) {  // R7: not the viewer's choice
      autoClosing = null;
      event.stopImmediatePropagation();
      return;
    }
    if (target.matches && target.matches("details.rc-trail-box")) {
      var summary = target.querySelector(":scope > summary");
      if (summary) summary.setAttribute("aria-expanded", target.open ? "true" : "false");
    }
  }, true);
  if (R7) {
    document.addEventListener("rc-list-updated", function () {  // live.js swapped the list
      shortWindowTeam();
      listFade();
    });
    window.addEventListener("resize", listFade);
  }

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

  if (openLatest()) return;
  trailToggles(document);
  watchThread();
  if (R7) {
    shortWindowTeam();
    listFade();
  }
  // Focus may arrive after this script runs (early in a navigation, or a window shown later): try again then.
  composerReady();
  window.addEventListener("load", composerReady);
  window.addEventListener("focus", composerReady, {once: true});
}());
