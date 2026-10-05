(function () {
  "use strict";
  function tick() { window.rcStatus.refresh().catch(function () {}); }
  tick();
  setInterval(tick, 5000);
}());
