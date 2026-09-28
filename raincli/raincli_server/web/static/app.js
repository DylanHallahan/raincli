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
    if (button && !message) {
      window.setTimeout(function () { button.disabled = true; }, 0);
    }
  });

  // Copy buttons: <button data-copy="input-id">.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    var field = document.getElementById(button.getAttribute("data-copy"));
    if (!field) return;
    var done = function () {
      var original = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = original; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(field.value).then(done, function () { field.select(); });
    } else {
      field.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* user can copy manually */ }
    }
  });

  // Select the whole token/link on focus so it is easy to copy by hand.
  document.querySelectorAll("input[readonly].mono").forEach(function (input) {
    input.addEventListener("focus", function () { input.select(); });
  });
})();
