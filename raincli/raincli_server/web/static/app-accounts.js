// The account switcher (protocol §23, A9; brief AS7): the "Signed in as" line opens the accounts this window holds,
// each with its unread count; one click switches. "Add an account" lists the granted accounts not yet added, and
// "Remove … from this app" (after a confirmation) ends that account's session here only.
//
// The window does the switching (A10). The page asks it through the window's sentinels (/app/local/switch,
// /app/local/add-account, /app/local/remove-account), and only this script can: each request carries the per-load
// nonce the window gives this page (window.__rcNonce, A14 L2). A plain link to a sentinel (in a message, say) has no
// nonce, so the window does nothing with it. Without the nonce (an older window, or the website) nothing happens.
// Ctrl+Shift+A switches to the next account. The list is swapped by live.js (rc-list-updated): an open switcher stays
// open, with focus kept on the same row where it can be.
(function () {
  "use strict";
  var body = document.body;
  var localUrl = body && body.getAttribute("data-local-url");
  if (!localUrl) return;
  var wasOpen = false, focusKey = null, pendingRemove = null;

  function nonce() { return typeof window.__rcNonce === "string" && window.__rcNonce ? window.__rcNonce : null; }
  function sentinel(name, params) {
    var n = nonce();
    if (!n) return false;  // not in a window that handles it: do nothing
    var q = Object.keys(params).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); });
    q.push("nonce=" + encodeURIComponent(n));
    location.assign(localUrl + name + "?" + q.join("&"));
    return true;
  }
  function parts() {
    var root = document.querySelector("[data-switcher]");
    return root ? {root: root, toggle: root.querySelector("[data-switcher-toggle]"),
                   panel: root.querySelector("[data-accounts]"), confirm: root.querySelector("[data-remove-confirm]")} : null;
  }
  function setOpen(open, focusFirst) {
    var p = parts();
    if (!p) return;
    p.panel.hidden = !open;
    p.toggle.setAttribute("aria-expanded", open ? "true" : "false");
    wasOpen = open;
    if (!open) { hideConfirm(p); return; }
    if (focusFirst) {
      var first = p.panel.querySelector(".rc-account[aria-current=true]") || p.panel.querySelector("button");
      if (first) first.focus();
    }
  }
  function hideConfirm(p) {
    pendingRemove = null;
    if (p && p.confirm) p.confirm.hidden = true;
  }
  function askRemove(button) {
    var p = parts();
    if (!p) return;
    pendingRemove = button.getAttribute("data-account-remove");
    p.confirm.querySelector("[data-remove-text]").textContent =
      "Remove " + button.getAttribute("data-account-name") + " from this app? Its messages stay; you can add it again.";
    p.confirm.hidden = false;
    p.confirm.querySelector("[data-remove-yes]").focus();
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    var p = parts();
    if (t.closest("[data-switcher-open]")) { if (p) { setOpen(p.panel.hidden, true); } return; }  // the rail avatar
    if (!p) return;
    if (t.closest("[data-switcher-toggle]")) { setOpen(p.panel.hidden, false); return; }
    var sw = t.closest("[data-account-switch]");
    if (sw) {
      if (sw.getAttribute("aria-current") === "true") { setOpen(false); p.toggle.focus(); return; }
      sentinel("switch", {account: sw.getAttribute("data-account-switch")});
      return;
    }
    var add = t.closest("[data-account-add]");
    if (add) { sentinel("add-account", {account: add.getAttribute("data-account-add")}); return; }
    var rm = t.closest("[data-account-remove]");
    if (rm) { askRemove(rm); return; }
    if (t.closest("[data-remove-yes]")) { if (pendingRemove) sentinel("remove-account", {account: pendingRemove}); return; }
    if (t.closest("[data-remove-no]")) { hideConfirm(p); return; }
    if (!p.panel.hidden && !p.root.contains(t)) setOpen(false);  // a click elsewhere closes it
  });
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && (e.key === "A" || e.key === "a")) {
      if (sentinel("switch", {next: "1"})) e.preventDefault();  // the window cycles to the next account
      return;
    }
    var p = parts();
    if (e.key === "Escape" && p && !p.panel.hidden) {
      e.preventDefault();
      setOpen(false);
      p.toggle.focus();
    }
  });
  // live.js swapped the list: keep an open switcher open and the focus on the same account where it can.
  document.addEventListener("focusin", function (e) {
    var row = e.target instanceof Element && e.target.closest("[data-accounts] button");
    focusKey = row ? (row.getAttribute("data-account-switch") || row.getAttribute("data-account-add") ||
                      row.getAttribute("data-account-remove")) : null;
  });
  document.addEventListener("rc-list-updated", function () {
    if (!wasOpen) return;
    var keep = focusKey;
    setOpen(true, false);
    if (keep) {
      var p = parts();
      var again = p && Array.prototype.find.call(p.panel.querySelectorAll("button"), function (b) {
        return [b.getAttribute("data-account-switch"), b.getAttribute("data-account-add"),
                b.getAttribute("data-account-remove")].indexOf(keep) >= 0;
      });
      if (again) again.focus({preventScroll: true});
    }
  });
}());
