// Shared by This computer and Settings: status, pause, open log and sign out.
(function () {
  "use strict";
  function label(value, fallback) { return value == null || value === "" ? fallback : value; }
  window.rcStatus = {
    refresh: function () {
      return rc.call("status").then(function (s) {
        rc.text("status", label(s.connection, "unknown"));
        rc.text("machine", label(s.machine, "not signed in"));
        rc.text("team", label(s.team, "—"));
        rc.text("version", label(s.version, "—"));
        rc.text("updates", label(s.updates, "—"));
        rc.text("routing", s.routing === "inbox-only" ? "inbox only" : label(s.routing, "—"));
        rc.text("pause", s.paused ? "Resume" : "Pause");
        var list = document.getElementById("agents");
        if (list) {
          list.textContent = "";
          (s.agents || []).forEach(function (a) {
            var li = document.createElement("li");
            var name = document.createElement("span");
            var dot = document.createElement("span");
            dot.className = "dot" + (a.reachability === "instant" || a.reachability === "next-turn" ? " ok" : "");
            dot.setAttribute("aria-hidden", "true");
            name.appendChild(dot); name.appendChild(document.createTextNode(a.name));
            var meta = document.createElement("span"); meta.className = "muted small";
            meta.textContent = [a.type, a.status, a.reachability || "listed"].join(" · ");
            li.appendChild(name); li.appendChild(meta); list.appendChild(li);
          });
          if (!list.children.length) { var li = document.createElement("li"); li.className = "muted"; li.textContent = "None reported yet."; list.appendChild(li); }
        }
        return s;
      });
    }
  };
  function on(id, fn) { var el = document.getElementById(id); if (el) el.addEventListener("click", fn); }
  on("pause", function () { rc.call("toggle_pause").then(window.rcStatus.refresh); });
  on("open-log", function () { rc.call("open_log"); });
  on("sign-out", function () {
    rc.call("sign_out").then(function (r) { if (r && r.message) rc.message(r.message, !r.ok); });
  });
}());
