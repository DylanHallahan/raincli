// The homepage (concept C "Paper"). No inline styles: everything is a class or a Web Animations call.
// - Header: a shadow once the page has scrolled; the phone menu closes after a link is chosen.
// - Sections fade in as they reach the screen (not under reduced motion, and never without this script).
// - The demo: a 16 s loop that just plays, but only while on screen and the tab is visible (off screen or hidden,
//   it pauses). Its first play starts at 11.4 s, on the finished thread, so the first frame is never empty. Under
//   reduced motion the CSS shows the final frame and nothing here touches it.
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
  var onScreen = false, started = false;

  function apply() {
    var hold = !onScreen || document.visibilityState !== "visible";
    if (!hold && !started) {
      demo.classList.add("play");
      // The first play starts on the finished thread, so the first frame is never empty.
      (demo.getAnimations ? demo.getAnimations({subtree: true}) : []).forEach(function (a) { a.currentTime = 11400; });
      started = true;
    }
    demo.classList.toggle("paused", hold && started);
    demo.setAttribute("data-demo-state", hold ? "waiting" : "playing");
  }
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
