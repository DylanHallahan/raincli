// The homepage demo (website Phase B), from demo-mock.html. The 22 s loop plays only while the demo is on screen
// and the tab is visible. Pause (WCAG 2.2.2) and Restart are buttons under the frame. Under reduced motion the CSS
// shows the final frame and hides the controls, so nothing here runs. No inline styles: pausing is a class.
(function () {
  "use strict";
  var demo = document.querySelector("[data-demo]");
  if (!demo) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var pause = demo.querySelector("[data-demo-pause]");
  var restart = demo.querySelector("[data-demo-restart]");
  var userPaused = false, onScreen = true;

  function apply() {
    var hold = userPaused || !onScreen || document.visibilityState !== "visible";
    demo.classList.toggle("paused", hold);
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
    demo.classList.remove("play");
    void demo.offsetWidth;  // a reflow, so the animations start again from 0
    demo.classList.add("play");
    if (userPaused) setUserPaused(false);
  });
  if (window.IntersectionObserver) {
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { onScreen = entry.isIntersecting; });
      apply();
    }, {threshold: 0.15}).observe(demo);
  }
  document.addEventListener("visibilitychange", apply);
  apply();
}());
