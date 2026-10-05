// Settings: routing policy (§16.5), trust (§16.12 C5), update mode (§14), pause, log, sign out.
(function () {
  "use strict";
  function check(name, value) { var el = document.getElementById(name + "-" + value); if (el) el.checked = true; }
  function load() {
    return rc.call("settings").then(function (s) {
      check("routing", s.routing); check("trust", s.trust_mode); check("update", s.update_mode);
      var list = document.getElementById("trusted");
      list.textContent = "";
      (s.trusted_senders || []).forEach(function (sender) {
        var li = document.createElement("li");
        var name = document.createElement("span"); name.textContent = sender;
        var remove = document.createElement("button");
        remove.type = "button"; remove.className = "btn btn-quiet"; remove.textContent = "Remove";
        remove.addEventListener("click", function () { save({trust_remove: sender}); });
        li.appendChild(name); li.appendChild(remove); list.appendChild(li);
      });
      window.rcStatus.refresh().catch(function () {});
    });
  }
  function save(change) {
    rc.call("save_settings", change).then(function (r) {
      rc.message(r.message, !r.ok);
      load();
    });
  }
  document.querySelectorAll("input[name=routing]").forEach(function (el) {
    el.addEventListener("change", function () { save({routing: el.value}); });
  });
  document.querySelectorAll("input[name=trust]").forEach(function (el) {
    el.addEventListener("change", function () { save({trust_mode: el.value}); });
  });
  document.querySelectorAll("input[name=update]").forEach(function (el) {
    el.addEventListener("change", function () { save({update_mode: el.value}); });
  });
  document.getElementById("trust-add-button").addEventListener("click", function () {
    var input = document.getElementById("trust-add");
    if (input.value.trim()) save({trust_add: input.value.trim()});
    input.value = "";
  });
  load();
}());
