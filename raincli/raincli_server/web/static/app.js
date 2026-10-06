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

  // Conversations open at the newest message (protocol §16.19 item 4). An #m-<id> anchor wins. On a reload
  // the reader stays at the newest message only if they were within 80 px of it; otherwise their place is
  // kept and "New messages" appears when the thread grew. Without JavaScript, links use #latest.
  (function () {
    var thread = document.querySelector("[data-thread]");
    if (!thread) return;
    var scroller = thread.closest("[data-thread-scroll]");
    var more = document.querySelector("[data-new-messages]");
    var key = "rc-thread:" + location.pathname;
    var count = thread.children.length;
    function lastMessage() { return thread.lastElementChild; }
    function viewBottom() { return scroller ? scroller.getBoundingClientRect().bottom : window.innerHeight; }
    function nearBottom() {
      var last = lastMessage();
      return !last || last.getBoundingClientRect().bottom - viewBottom() <= 80;
    }
    function position() { return scroller ? scroller.scrollTop : window.scrollY; }
    function setPosition(top) { if (scroller) { scroller.scrollTop = top; } else { window.scrollTo(0, top); } }
    function toNewest() {
      var last = lastMessage();
      if (last) last.scrollIntoView({block: "end"});
      if (more) more.hidden = true;
    }
    function remember() {
      try {
        sessionStorage.setItem(key, JSON.stringify({near: nearBottom(), top: position(), count: count}));
      } catch (e) { /* no storage: every open goes to the newest message */ }
    }
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(key) || "null"); } catch (e) { saved = null; }
    var nav = window.performance && performance.getEntriesByType ? performance.getEntriesByType("navigation")[0] : null;
    var reload = !!(nav && (nav.type === "reload" || nav.type === "back_forward"));
    var hash = location.hash;
    var anchored = /^#m-[0-9a-fA-F-]+$/.test(hash) && document.getElementById(hash.slice(1));
    if (anchored) {
      anchored.scrollIntoView({block: "center"});
    } else if (reload && saved && !saved.near && typeof saved.top === "number") {
      setPosition(saved.top);
      if (more && count > (saved.count || 0)) more.hidden = false;
    } else {
      toNewest();
    }
    if (more) more.addEventListener("click", function (event) { event.preventDefault(); toNewest(); remember(); });
    (scroller || window).addEventListener("scroll", function () {
      if (more && !more.hidden && nearBottom()) more.hidden = true;
      remember();
    }, {passive: true});
    window.addEventListener("pagehide", remember);
    remember();
  }());
})();
