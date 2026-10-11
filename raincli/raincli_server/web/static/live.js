// Live updates in app mode (protocol §19.1, §19.2). One GET /app/live every 5 s while the window is visible;
// none while it is hidden or minimised, and one at once when it is shown again. After a network error it waits
// 30 s, and it honours the server's retry_after. It never shows an error dialog.
//
// Fragments come only from this same-origin endpoint, rendered by the server's own templates and Markdown
// renderer. They are parsed with DOMParser (an inert document: nothing runs) and imported node by node.
//
// Read state (§19.2): a message to you is acknowledged only when the window has focus and at least half of the
// message has been on screen (POST /app/ack). Loading by poll never acknowledges anything.
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
  function addMessages(items) {
    if (!thread || !items.length) return;
    var follow = nearBottom();
    var added = null;
    items.forEach(function (item) {
      if (item.seq > after) after = item.seq;
      if (document.getElementById("m-" + item.id)) return;
      var li = firstElement(item.html);
      if (!li) return;
      li.classList.add("rc-arrived");
      thread.appendChild(li);
      watch(li);
      added = li;
    });
    if (!added) return;
    if (follow) {
      added.scrollIntoView({block: "end"});
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
        if (entry.isIntersecting && entry.intersectionRatio >= 0.5) { visible[id] = entry.target; } else { delete visible[id]; }
      });
      markSeen();
    }, {root: scroller || null, threshold: [0, 0.5, 1]});
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
      .then(function (r) { return r.ok ? r.json() : {acked: []}; })
      .then(function (reply) {
        ids.forEach(function (id) {
          var li = visible[id] || document.getElementById("m-" + id);
          if (li) { li.removeAttribute("data-unread"); if (observer) observer.unobserve(li); }
          delete visible[id];
        });
        if (reply.acked && reply.acked.length) poll(true);  // the list's unread count moves at once
      }, function () { ids.forEach(function (id) { delete queued[id]; }); });
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
        apply(reply);
        schedule(Math.max(INTERVAL, (Number(reply.retry_after) || 0) * 1000));
      }, function () {
        inFlight = false;
        schedule(BACKOFF);  // quietly: no dialog, the page stays as it is
      });
  }
  function apply(reply) {
    if (reply.now) statesSince = reply.now;
    addMessages(reply.messages || []);
    updateStates(reply.states || []);
    if (reply.header) replaceChildren(document.querySelector("[data-live-header]"), reply.header.html);
    var slot = document.querySelector("[data-live-warning]");
    if (slot) replaceChildren(slot, reply.warning ? reply.warning.html : "");
    if (reply.list && reply.list.html) {
      var old = document.querySelector(".rc-list");
      var fresh = firstElement(reply.list.html);
      if (old && fresh) {
        var oldScroll = old.querySelector("ul"), top = oldScroll ? oldScroll.scrollTop : 0;
        old.parentNode.replaceChild(fresh, old);
        var newScroll = fresh.querySelector("ul");
        if (newScroll) newScroll.scrollTop = top;
        document.dispatchEvent(new CustomEvent("rc-list-updated"));
      }
    }
    if (reply.list_rev) listRev = reply.list_rev;
    markSeen();
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { poll(true); } else if (timer) { clearTimeout(timer); timer = null; }
  });
  window.rcLive = {poll: function () { poll(true); }};  // for tests and a manual refresh
  schedule(INTERVAL);
}());
