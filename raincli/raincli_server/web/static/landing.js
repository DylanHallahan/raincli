// The homepage (concept C "Paper"). No inline styles: everything is a class or a Web Animations call.
// - Header: a shadow once the page has scrolled; the phone menu closes after a link is chosen.
// - Sections fade in as they reach the screen (not under reduced motion, and never without this script).
// - The demo: a 16 s loop that plays only while on screen and the tab is visible. Its first play starts at 11.4 s,
//   on the finished thread, so the first frame is never empty; Restart starts from 0. Pause (WCAG 2.2.2) and Restart
//   are buttons under the frame. Under reduced motion the CSS shows the final frame and hides the controls, and
//   nothing here touches it.
(function () {
  "use strict";
  var root = document.documentElement;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var header = document.querySelector("[data-site-header]");
  if (header) {
    var update = function () { header.classList.toggle("scrolled", window.scrollY > 0); };
    window.addEventListener("scroll", update, {passive: true});
    update();
    var menu = header.querySelector(".menu");
    if (menu) menu.addEventListener("click", function (e) { if (e.target.closest("a")) menu.open = false; });
  }

  var reveal = Array.prototype.slice.call(document.querySelectorAll(".rv"));
  if (!reduced && window.IntersectionObserver) {
    root.classList.add("js");
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("in"); io.unobserve(entry.target); }
      });
    }, {threshold: 0.12});
    reveal.forEach(function (el) { io.observe(el); });
  }

  var demo = document.querySelector("[data-demo]");
  if (!demo || reduced) return;
  var pause = demo.querySelector("[data-demo-pause]");
  var restart = demo.querySelector("[data-demo-restart]");
  var userPaused = false, onScreen = false, started = false;

  function animations() { return demo.getAnimations ? demo.getAnimations({subtree: true}) : []; }
  function start(at) {
    demo.classList.remove("play");
    void demo.offsetWidth;  // a reflow, so the animations start again
    demo.classList.add("play");
    animations().forEach(function (a) { a.currentTime = at; });
    started = true;
  }
  function apply() {
    var hold = userPaused || !onScreen || document.visibilityState !== "visible";
    if (!hold && !started) start(11400);
    demo.classList.toggle("paused", hold && started);
    demo.setAttribute("data-demo-state", hold ? (userPaused ? "paused" : "waiting") : "playing");
  }
  function setUserPaused(on) {
    userPaused = on;
    pause.setAttribute("aria-pressed", on ? "true" : "false");
    pause.textContent = on ? "Play" : "Pause";
    apply();
  }
  if (pause) pause.addEventListener("click", function () { setUserPaused(!userPaused); });
  if (restart) restart.addEventListener("click", function () {
    start(0);
    if (userPaused) setUserPaused(false); else apply();
  });
  if (window.IntersectionObserver) {
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { onScreen = entry.isIntersecting; });
      apply();
    }, {threshold: 0.15}).observe(demo);
  } else {
    onScreen = true;
  }
  document.addEventListener("visibilitychange", apply);
  apply();
}());
