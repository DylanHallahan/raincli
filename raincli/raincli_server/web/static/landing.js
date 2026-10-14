// The homepage (concept C "Paper"). No inline styles: everything is a class or a Web Animations call.
// - Header: a shadow once the page has scrolled; the phone menu closes after a link is chosen.
// - Sections fade in as they reach the screen (not under reduced motion, and never without this script).
// - The demo: a 16 s sequence that just plays, with no controls, only while on screen and the tab is visible (off
//   screen or hidden, it pauses). It starts at 11.4 s, on the finished thread, so the first frame is never empty;
//   after two plays it stops on the finished frame. Under reduced motion the CSS shows that frame straight away
//   and nothing here touches it.
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
  // Two plays, then it holds: the first play joins on the finished thread (11.4 s, the tail of a loop), then the
  // whole 16 s sequence runs twice, and the demo stops on the second play's finished frame (13 s into it). Time is
  // the animations' own clock, so a pause off screen or in a hidden tab delays the stop.
  var LOOP = 16000, FINISHED = 13000, STOP_AT = FINISHED + 2 * LOOP;
  var onScreen = false, started = false, done = false, timer = null;

  function animations() { return demo.getAnimations ? demo.getAnimations({subtree: true}) : []; }
  function stop() {
    // Without .play nothing animates, and the CSS shows the finished frame (as under reduced motion).
    done = true;
    clearInterval(timer);
    demo.classList.remove("play", "paused");
    demo.setAttribute("data-demo-state", "done");
  }
  function check() {
    var all = animations();
    if (all.length && all[0].currentTime >= STOP_AT) stop();
  }
  function apply() {
    if (done) return;
    var hold = !onScreen || document.visibilityState !== "visible";
    if (!hold && !started) {
      demo.classList.add("play");
      // The first play starts on the finished thread, so the first frame is never empty.
      animations().forEach(function (a) { a.currentTime = 11400; });
      started = true;
      timer = setInterval(check, 200);
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
