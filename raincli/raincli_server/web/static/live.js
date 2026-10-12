// Live updates in app mode (protocol §19.1, §19.2). One GET /app/live every 5 s while the window is visible;
// none while it is hidden or minimised, and one at once when it is shown again. After a network error it waits
// 30 s, and it honours the server's retry_after. It never shows an error dialog.
//
// Fragments come only from this same-origin endpoint, rendered by the server's own templates and Markdown
// renderer. They are parsed with DOMParser (an inert document: nothing runs) and imported node by node.
//
// Read state (§19.2, §19.8 M3): a message to you is acknowledged only when the window has focus and its visible
// height is at least min(half the message, half the viewport) (POST /app/ack). Loading by poll never acks.
// The cursors are hints (§19.8 M1): each reply may repeat recent messages and current states; both merge by id.
(function () {
  "use strict";
  var body = document.body;
  var liveUrl = body && body.getAttribute("data-live-url");
  if (!liveUrl || !window.fetch || !window.DOMParser) return;
  var ackUrl = body.getAttribute("data-ack-url");
  var config = document.querySelector("[data-live]");
  var conversation = config ? config.getAttribute("data-conversation") : "";
  var after = config ? Number(config.getAttribute("data-after")) || 0 : 0;
  var statesSince = config ? config.getAttribute("data-states-since") || "" : "";
  var csrf = config ? config.getAttribute("data-csrf") || "" : (body.getAttribute("data-csrf") || "");
  var listRev = "";
  var INTERVAL = 5000, BACKOFF = 30000;
  var timer = null, inFlight = false;
  var parser = new DOMParser();

  function fragmentNodes(html) {
    var doc = parser.parseFromString("<!doctype html><body>" + html, "text/html");
    return Array.prototype.map.call(doc.body.childNodes, function (n) { return document.importNode(n, true); });
  }
  function firstElement(html) {
    var nodes = fragmentNodes(html);
    for (var i = 0; i < nodes.length; i++) if (nodes[i].nodeType === 1) return nodes[i];
    return null;
  }
  function replaceChildren(target, html) {
    if (!target) return;
    while (target.firstChild) target.removeChild(target.firstChild);
    fragmentNodes(html || "").forEach(function (n) { target.appendChild(n); });
  }

  // The thread ------------------------------------------------------------------------------------------
  var thread = document.querySelector("[data-thread]");
  var scroller = thread && thread.closest("[data-thread-scroll]");
  var more = document.querySelector("[data-new-messages]");
  function nearBottom() {
    var last = thread && thread.lastElementChild;
    if (!last) return true;
    var bottom = scroller ? scroller.getBoundingClientRect().bottom : window.innerHeight;
    return last.getBoundingClientRect().bottom - bottom <= 80;
  }
  function addMessages(items, own) {
    if (!thread || !items.length) return;
    var follow = own || nearBottom();  // the viewer's own new message always scrolls into view (§20.6 L4)
    var added = null;
    items.forEach(function (item) {
      if (item.seq > after) after = item.seq;
      if (document.getElementById("m-" + item.id)) return;  // merged by id (§19.8 M1)
      var li = firstElement(item.html);
      if (!li) return;
      li.classList.add("rc-arrived");
      // In seq order: a message that committed late, with a lower seq, goes in its place, not at the end.
      var next = null;
      for (var el = thread.lastElementChild; el; el = el.previousElementSibling) {
        if (Number(el.getAttribute("data-seq")) > item.seq) { next = el; } else { break; }
      }
      thread.insertBefore(li, next);
      watch(li);
      if (!next) added = li;
    });
    if (!added) return;
    if (follow) {
      // Inside the thread's scroller only: scrollIntoView would also scroll the document (the shell bug).
      if (scroller) { scroller.scrollTop += added.getBoundingClientRect().bottom - scroller.getBoundingClientRect().bottom; }
      else { added.scrollIntoView({block: "end"}); }
      if (more) more.hidden = true;
    } else if (more) {
      more.hidden = false;  // the existing "New messages ↓" control (§16.19 item 4)
    }
  }
  function updateStates(items) {
    items.forEach(function (item) {
      var old = document.querySelector('[data-trail="' + item.id + '"]');
      var fresh = firstElement(item.html);
      if (!old || !fresh) return;
      // Idempotent (§19.8 M1): the same state comes on every poll until it is final; only a change is applied.
      if (old.getAttribute("data-state") === fresh.getAttribute("data-state") &&
          old.querySelector(".rc-trail-steps").textContent === fresh.querySelector(".rc-trail-steps").textContent) return;
      var open = old.querySelector("details[open]");
      if (open) { var d = fresh.querySelector("details"); if (d) d.open = true; }
      fresh.classList.add("rc-changed");
      old.parentNode.replaceChild(fresh, old);
    });
  }

  // Read when focused and half on screen (§19.2) ---------------------------------------------------------
  var visible = {}, queued = {}, observer = null;
  if (window.IntersectionObserver && ackUrl) {
    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var id = entry.target.id.slice(2);
        // §19.8 M3: seen when its visible height is at least min(half the message, half the viewport), so a long
        // message counts once it fills half the view.
        var view = entry.rootBounds ? entry.rootBounds.height : window.innerHeight;
        var need = Math.min(entry.boundingClientRect.height * 0.5, view * 0.5);
        if (entry.isIntersecting && entry.intersectionRect.height >= need - 0.5) { visible[id] = entry.target; } else { delete visible[id]; }
      });
      markSeen();
    }, {root: scroller || null, threshold: Array.from({length: 21}, function (_, i) { return i / 20; })});
  }
  function watch(li) {
    if (observer && li.hasAttribute("data-unread")) observer.observe(li);
  }
  if (thread) Array.prototype.forEach.call(thread.querySelectorAll("li[data-unread]"), watch);
  function markSeen() {
    if (!document.hasFocus() || document.visibilityState !== "visible") return;
    var ids = Object.keys(visible).filter(function (id) { return !queued[id]; }).slice(0, 100);
    if (!ids.length) return;
    ids.forEach(function (id) { queued[id] = true; });
    fetch(ackUrl, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
                   body: JSON.stringify({csrf_token: csrf, ids: ids})})
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);  // §19.9 F8: not acked, so not read
        return r.json();
      })
      .then(function (reply) {
        ids.forEach(function (id) {
          var li = visible[id] || document.getElementById("m-" + id);
          if (li) { li.removeAttribute("data-unread"); if (observer) observer.unobserve(li); }
          delete visible[id];
        });
        if (reply.acked && reply.acked.length) poll(true);  // the list's unread count moves at once
      }, function () {
        ids.forEach(function (id) { delete queued[id]; });  // still unread and still observed: tried again later
      });
  }
  window.addEventListener("focus", markSeen);

  // The poll ----------------------------------------------------------------------------------------------
  function schedule(delay) {
    if (timer) clearTimeout(timer);
    timer = null;
    if (document.visibilityState !== "visible") return;  // stopped while hidden or minimised
    timer = setTimeout(function () { poll(false); }, delay);
  }
  function poll(soon) {
    if (inFlight) { if (soon) schedule(500); return; }
    if (document.visibilityState !== "visible") return;
    inFlight = true;
    var q = "after=" + encodeURIComponent(after) + "&list_rev=" + encodeURIComponent(listRev);
    if (conversation) q += "&conversation=" + encodeURIComponent(conversation) + "&states_since=" + encodeURIComponent(statesSince);
    fetch(liveUrl + "?" + q, {credentials: "same-origin", headers: {"Accept": "application/json"}, cache: "no-store"})
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (reply) {
        inFlight = false;
        try {
          apply(reply);
        } finally {  // §19.9 F5: an error while applying never stops the polling
          schedule(Math.max(INTERVAL, (Number(reply.retry_after) || 0) * 1000));
        }
      }, function () {
        inFlight = false;
        schedule(BACKOFF);  // quietly: no dialog, the page stays as it is
      });
  }
  // §19.9 F4: a part is replaced only when its HTML changed, so a hovered tooltip stays and an unchanged
  // role="status" warning is never announced again.
  function replaceIfChanged(target, part) {
    if (!target || !part) return;
    if (part.sig && target.getAttribute("data-live-sig") === part.sig) return;
    replaceChildren(target, part.html);
    if (part.sig) target.setAttribute("data-live-sig", part.sig);
  }

  // §19.9 F3: the list swap keeps keyboard focus (matched by link, summary or data attribute) and the scroll
  // position of every scrollable part of the list.
  function focusKey(el, root) {
    if (!el || !root.contains(el) || el === root) return null;
    if (el.getAttribute("href")) return {sel: '[href="' + CSS.escape(el.getAttribute("href")) + '"]'};
    if (el.tagName === "SUMMARY") {
      var details = el.closest("details");
      if (details && details.getAttribute("data-team")) return {sel: 'details[data-team="' + details.getAttribute("data-team") + '"] > summary'};
      return {sel: "summary"};
    }
    return null;
  }
  function scrolls(root) {
    var out = [];
    Array.prototype.forEach.call(root.querySelectorAll("*"), function (el) {
      if (el.scrollHeight > el.clientHeight && el.scrollTop) out.push({cls: el.className, top: el.scrollTop});
    });
    return out;
  }
  function swapList(html) {
    var old = document.querySelector(".rc-list");
    var fresh = firstElement(html);
    if (!old || !fresh) return;
    var focus = focusKey(document.activeElement, old);
    var kept = scrolls(old);
    old.parentNode.replaceChild(fresh, old);
    document.dispatchEvent(new CustomEvent("rc-list-updated"));  // day groups and the Team state re-apply first
    kept.forEach(function (k) {
      var el = k.cls && fresh.getElementsByClassName(String(k.cls).split(" ")[0])[0];
      if (el) el.scrollTop = k.top;
    });
    if (focus) {
      var target = fresh.querySelector(focus.sel);
      if (target) target.focus({preventScroll: true});
    }
  }

  function apply(reply) {
    if (reply.now) statesSince = reply.now;
    addMessages(reply.messages || []);
    updateStates(reply.states || []);
    replaceIfChanged(document.querySelector("[data-live-header]"), reply.header);
    if (conversation) replaceIfChanged(document.querySelector("[data-live-warning]"), reply.warning || {html: "", sig: "none"});
    if (reply.list && reply.list.html) swapList(reply.list.html);
    if (reply.list_rev) listRev = reply.list_rev;
    markSeen();
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { poll(true); } else if (timer) { clearTimeout(timer); timer = null; }
  });
  window.rcLive = {
    poll: function () { poll(true); },  // for tests, a manual refresh and right after a send
    // §20.6 L4: a message sent in place goes through the same merge by id as the poll (skipped when #m-<id> is
    // already there, inserted in seq order, animated once), then an immediate poll moves the list.
    merge: function (item) { addMessages([item], true); poll(true); }
  };
  schedule(INTERVAL);
}());
