// RainCLI web: small progressive enhancements. Everything works without JavaScript.
(function () {
  "use strict";

  // Confirm destructive actions (revoke, rotate) before submitting.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Avoid accidental double submits; the server is idempotent anyway.
    var button = form.querySelector('button[type="submit"]');
    if (button && !message && !form.hasAttribute("data-download")) {
      window.setTimeout(function () { button.disabled = true; }, 0);
    }
  });

  // Copy buttons: <button data-copy="input-id">.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    var field = document.getElementById(button.getAttribute("data-copy"));
    if (!field) return;
    var status = document.getElementById(button.getAttribute("data-copy-status"));
    var fallback = function () {
      field.focus();
      field.select();
      if (status) status.textContent = "Text selected. Press Ctrl+C or Command+C to copy.";
    };
    var done = function () {
      if (status) status.textContent = "Copied. Paste it into your coding agent.";
      var original = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = original; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(field.value).then(done, fallback);
    } else {
      fallback();
      try { if (document.execCommand("copy")) done(); } catch (e) { /* selection stays available */ }
    }
  });

  // Select the whole token/link on focus so it is easy to copy by hand.
  document.querySelectorAll("input[readonly].mono").forEach(function (input) {
    input.addEventListener("focus", function () { input.select(); });
  });
  // App mode: name the chosen attachments next to the clip.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches('.rc-box input[type="file"]')) return;
    var out = input.form && input.form.querySelector(".rc-files");
    if (out) out.textContent = Array.prototype.map.call(input.files, function (f) { return f.name; }).join(", ");
  });
})();
