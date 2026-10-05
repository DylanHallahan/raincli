(function () {
  "use strict";
  document.getElementById("retry").addEventListener("click", function () {
    rc.message("Trying again…");
    rc.call("retry").then(function (r) { if (!r.ok) rc.message(r.message || "Still can't reach RainCLI.", true); });
  });
}());
