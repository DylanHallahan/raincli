// Local time (protocol §17.1): every <time datetime> in the viewer's own time zone and locale, through Intl.
// The visible label is relative while recent ("now", "5 minutes ago"), then the local time or date; the title
// is the full local date and time with seconds and the zone. Times added later (a new message, a status
// refresh) are formatted as they appear. Without JavaScript the server's UTC text stays as it is.
// The same file ships with the Windows app's local pages; a test keeps the two copies byte-identical.
(function () {
  "use strict";
  if (typeof Intl === "undefined" || !Intl.DateTimeFormat || !document.querySelectorAll) return;
  var MINUTE = 60000, HOUR = 60 * MINUTE;
  var full = new Intl.DateTimeFormat(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "long"
  });
  var clock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  var day = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  var dated = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  var ymd = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit" });  // the local date, to compare
  var relative = Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;

  function label(when, now) {
    var ago = now - when;
    if (relative && ago > -MINUTE && ago < MINUTE) return relative.format(0, "second");
    if (relative && ago >= MINUTE && ago < HOUR) return relative.format(-Math.floor(ago / MINUTE), "minute");
    if (ymd.format(when) === ymd.format(now)) return clock.format(when);
    if (when.getFullYear() === now.getFullYear()) return day.format(when);
    return dated.format(when);
  }

  function format(el, now) {
    var value = el.getAttribute("datetime");
    var when = value ? new Date(value) : null;
    if (!when || isNaN(when.getTime())) return;
    var text = label(when, now || new Date());
    if (el.textContent !== text) el.textContent = text;
    var title = full.format(when);
    if (el.getAttribute("title") !== title) el.setAttribute("title", title);
    el.setAttribute("data-local", "1");
  }

  function formatAll(root) {
    var now = new Date();
    if (root.matches && root.matches("time[datetime]")) format(root, now);
    var found = root.querySelectorAll ? root.querySelectorAll("time[datetime]") : [];
    for (var i = 0; i < found.length; i++) format(found[i], now);
  }

  function start() {
    formatAll(document);
    if (window.MutationObserver) {
      new MutationObserver(function (changes) {
        changes.forEach(function (change) {
          if (change.type === "attributes") { format(change.target); return; }
          for (var i = 0; i < change.addedNodes.length; i++) {
            if (change.addedNodes[i].nodeType === 1) formatAll(change.addedNodes[i]);
          }
        });
      }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["datetime"] });
    }
    setInterval(function () { formatAll(document); }, 30000);  // relative labels move on
  }

  window.rcLocalTime = { format: format, formatAll: formatAll };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}());
