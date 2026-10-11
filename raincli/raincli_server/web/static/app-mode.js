// App mode only (protocol §20, §20.6 M4): behaviour new in v0.5.5, loaded by base_app.html after live.js. Each part
// is its own function and everything degrades to the plain pages without JavaScript. The shared app.js keeps
// the v0.5.4 list code (day groups, the Team open state); this file adds to it and never replaces its hooks.
(function () {
  "use strict";
  var body = document.body;
  var TEAM_KEY = "rc-team-open:";  // app.js's key for the Team section's open state, per viewer

  // §20.6 M1 (P2): Inbox opens the latest conversation, the first in the list (ordered by last seq, archived ones
  // excluded), when the list is visible: above the app-mode breakpoint. Below it, Inbox shows the list. A toast
  // opens its own thread directly (the handoff's ``next``), so it never passes through here.
  function openLatest() {
    if (!body.classList.contains("rc-inbox")) return false;
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
  document.addEventListener("rc-list-updated", function () {  // live.js swapped the list
    shortWindowTeam();
    listFade();
  });
  window.addEventListener("resize", listFade);

  if (openLatest()) return;
  trailToggles(document);
  watchThread();
  shortWindowTeam();
  listFade();
  // Focus may arrive after this script runs (early in a navigation, or a window shown later): try again then.
  composerReady();
  window.addEventListener("load", composerReady);
  window.addEventListener("focus", composerReady, {once: true});
}());
