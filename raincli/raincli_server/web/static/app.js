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

  // App mode (protocol §19.5): day headings in the conversation list, in this computer's local time, and the
  // Team section's open or closed state, remembered per viewer. Both run again after live.js swaps the list
  // ("rc-list-updated"). Without JavaScript there are no headings and the Team section starts open.
  var DAY = new Intl.DateTimeFormat("en-CA", {year: "numeric", month: "2-digit", day: "2-digit"});
  function dayGroups(root) {
    var list = root.querySelector("[data-day-groups]");
    if (!list) return;
    Array.prototype.forEach.call(list.querySelectorAll(":scope > .rc-day"), function (li) { li.remove(); });
    var now = new Date();
    var today = DAY.format(now);
    var yesterday = DAY.format(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 12));
    var last = null;
    Array.prototype.forEach.call(list.querySelectorAll(":scope > .rc-conv-item"), function (item) {
      var time = item.querySelector(".rc-conv-time time[datetime]");
      var when = time ? new Date(time.getAttribute("datetime")) : null;
      if (!when || isNaN(when.getTime())) return;
      var local = DAY.format(when);
      var label = local === today ? "Today" : local === yesterday ? "Yesterday" : "Earlier";
      if (label === last) return;
      last = label;
      var head = document.createElement("li");
      head.className = "rc-day";
      head.setAttribute("role", "presentation");
      var h = document.createElement("h2");
      h.className = "rc-day-h";
      h.textContent = label;
      head.appendChild(h);
      list.insertBefore(head, item);
    });
  }
  function teamKey(details) { return "rc-team-open:" + details.getAttribute("data-team"); }
  function teamState(root) {
    Array.prototype.forEach.call(root.querySelectorAll("details[data-team]"), function (details) {
      var saved = null;
      try { saved = localStorage.getItem(teamKey(details)); } catch (e) { saved = null; }
      if (saved === "0") details.open = false;
      else if (saved === "1") details.open = true;
      details.addEventListener("toggle", function () {
        try { localStorage.setItem(teamKey(details), details.open ? "1" : "0"); } catch (e) { /* convenience only */ }
      });
    });
  }
  function enhanceList() {
    var root = document.querySelector(".rc-list");
    if (!root) return;
    dayGroups(root);
    teamState(root);
  }
  enhanceList();
  document.addEventListener("rc-list-updated", enhanceList);
  window.setInterval(function () {  // the headings move on at midnight
    var root = document.querySelector(".rc-list");
    if (root) dayGroups(root);
  }, 60000);

  // The composer (§19.7): the textarea grows from 1 to 8 lines (CSS caps it) and Ctrl+Enter sends; Enter adds
  // a new line. Without JavaScript the form still sends with its button.
  function grow(area) {
    area.style.height = "auto";
    area.style.height = area.scrollHeight + "px";
  }
  document.querySelectorAll("textarea[data-grow]").forEach(function (area) {
    grow(area);
    area.addEventListener("input", function () { grow(area); });
    area.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && area.form) {
        event.preventDefault();
        if (area.form.reportValidity()) {
          if (area.form.requestSubmit) { area.form.requestSubmit(); } else { area.form.submit(); }
        }
      }
    });
  });

  // The sender warning on the compose page (§19.6): once a machine or agent is chosen, say whether it is
  // connecting and picking up. Display only.
  document.querySelectorAll("input[data-recipient][data-warning-url]").forEach(function (input) {
    var slot = input.form && input.form.querySelector("[data-recipient-warning]");
    if (!slot || !window.fetch || !window.DOMParser) return;
    var last = input.value, pending = null;
    function check() {
      var value = input.value.trim();
      if (value === last) return;
      last = value;
      fetch(input.getAttribute("data-warning-url") + "?to=" + encodeURIComponent(value),
            {credentials: "same-origin", cache: "no-store"})
        .then(function (r) { return r.ok ? r.text() : ""; })
        .then(function (html) {
          if (input.value.trim() !== value) return;  // typed on since
          var doc = new DOMParser().parseFromString("<!doctype html><body>" + html, "text/html");
          while (slot.firstChild) slot.removeChild(slot.firstChild);
          Array.prototype.forEach.call(doc.body.childNodes, function (n) { slot.appendChild(document.importNode(n, true)); });
        }, function () { /* quietly: the warning is a convenience */ });
    }
    input.addEventListener("change", check);
    input.addEventListener("input", function () { clearTimeout(pending); pending = setTimeout(check, 400); });
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
