// The app's bundled pages talk to the app only through pywebview's js_api, with the per-load nonce the
// app passes to this origin (protocol §16.12 C4). No credentials ever come back.
(function () {
  "use strict";
  var waiting = [];
  function ready(fn) {
    if (window.__rcNonce && window.pywebview && window.pywebview.api) { fn(); return; }
    waiting.push(fn);
  }
  function flush() {
    if (!(window.__rcNonce && window.pywebview && window.pywebview.api)) return;
    var fns = waiting; waiting = [];
    fns.forEach(function (fn) { fn(); });
  }
  window.addEventListener("pywebviewready", flush);
  window.addEventListener("rc-nonce", flush);
  window.rc = {
    call: function (name) {
      var args = Array.prototype.slice.call(arguments, 1);
      return new Promise(function (resolve, reject) {
        ready(function () {
          var fn = window.pywebview.api[name];
          if (typeof fn !== "function") { reject(new Error("unknown action " + name)); return; }
          fn.apply(null, [window.__rcNonce].concat(args)).then(resolve, reject);
        });
      });
    },
    // A one-shot read (no side effects): each attempt waits 8 s, then asks again, up to 4 times. pywebview
    // re-injects its bridge when a navigation the app cancelled completes (the /app/local sentinel), which
    // drops the reply to a call already in flight; a read must not hang on that.
    read: function (name) {
      var args = Array.prototype.slice.call(arguments, 1);
      return new Promise(function (resolve, reject) {
        var done = false, attempts = 0;
        function attempt() {
          attempts += 1;
          var timer = setTimeout(function () {
            if (done) return;
            if (attempts < 4) { attempt(); } else { done = true; reject(new Error("no answer")); }
          }, 8000);
          window.rc.call.apply(null, [name].concat(args)).then(function (value) {
            clearTimeout(timer);
            if (!done) { done = true; resolve(value); }
          }, function (error) {
            clearTimeout(timer);
            if (!done) { done = true; reject(error); }
          });
        }
        attempt();
      });
    },
    text: function (id, value) { var el = document.getElementById(id); if (el) el.textContent = value == null ? "" : String(value); },
    show: function (id, visible) { var el = document.getElementById(id); if (el) el.classList.toggle("hidden", !visible); },
    message: function (text, isError) {
      var el = document.getElementById("message");
      if (!el) return;
      el.textContent = text || "";
      el.classList.toggle("error", !!isError);
    }
  };
  // The rail: hosted sections go through the app (handoff), local ones are plain links.
  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-open]");
    if (!target) return;
    event.preventDefault();
    window.rc.call("open", target.getAttribute("data-open")).catch(function () {});
  });
}());
