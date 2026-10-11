// Send in place (protocol §20.2, §20.6 M2, M4, L4, L5), app mode only: the conversation's composer posts to
// /app/send and the returned message goes into the thread through live.js's merge by id, with no page load.
// Without JavaScript, or without fetch, the form posts and redirects as before.
//
// The message id (M2): one per draft, kept across retries after an unknown outcome (a network error, a 5xx or a
// 429), and new after a success or a definite rejection. A 409 means an earlier version of this draft was
// already stored: that message is shown and the edited draft stays, under a new id.
(function () {
  "use strict";
  var form = document.querySelector("form[data-send-in-place]");
  if (!form || !window.fetch || !window.FormData) return;
  var url = form.getAttribute("data-send-in-place");
  var idField = form.querySelector('input[name="message_id"]');
  var body = form.querySelector("textarea[name='body']");
  var files = form.querySelector('input[type="file"]');
  var names = form.querySelector(".rc-files");
  var button = form.querySelector('button[type="submit"]');
  var status = document.createElement("p");
  status.className = "rc-send-status";
  status.setAttribute("role", "status");
  status.hidden = true;
  form.insertBefore(status, form.firstChild);
  var sending = false;
  var GENERIC = "Couldn't send. Check your connection and try again.";

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  function say(text, isError) {
    status.textContent = text || "";
    status.hidden = !text;
    status.classList.toggle("rc-send-error", !!isError);
  }
  function show(reply) {
    if (reply && reply.message_html && window.rcLive && window.rcLive.merge) {
      window.rcLive.merge({id: reply.id, seq: reply.seq, html: reply.message_html});
    }
  }
  function done(reply) {
    show(reply);
    // A reply goes to its message's sender once; the next message goes to the conversation, as after a page load.
    var parent = form.querySelector('input[name="in_reply_to"]');
    if (parent) {
      parent.remove();
      var to = form.querySelector(".rc-to strong");
      if (to) to.textContent = form.getAttribute("data-default-to") || to.textContent;
      if (window.history && history.replaceState) history.replaceState(null, "", location.pathname);
    }
    body.value = "";
    if (files) files.value = "";
    if (names) names.textContent = "";  // the chosen file names stay until the send succeeds (§20.6 L8)
    idField.value = newId();
    say("");
  }
  function finish() {
    sending = false;
    if (button) button.disabled = false;
    form.removeAttribute("aria-busy");
  }

  // On the form itself, so this runs before app.js's document-level submit handler (which skips events that
  // are defaultPrevented and forms marked data-send-in-place, §20.6 M4).
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) return;
    if (!form.reportValidity()) return;
    sending = true;
    form.setAttribute("aria-busy", "true");
    say("");
    var data = new FormData(form);
    fetch(url, {method: "POST", body: data, credentials: "same-origin", headers: {"Accept": "application/json"}})
      .then(function (r) {
        return r.json().then(function (reply) { return {status: r.status, reply: reply}; },
                             function () { return {status: r.status, reply: null}; });
      })
      .then(function (res) {
        var reply = res.reply, error = reply && reply.error;
        if (res.status >= 200 && res.status < 300 && reply && !error) {
          done(reply);
        } else if (res.status === 409 && reply && reply.message_html) {
          // An earlier version of this draft was stored (the response to it was lost): show it, keep the edit.
          show(reply);
          idField.value = newId();
          say("Your earlier version was already sent.", false);
        } else {
          // L5: the server's friendly message when there is one, else the generic line. The draft stays.
          say(error && error.message ? error.message : GENERIC, true);
          // M2: after a definite rejection (a 409 here is an id someone else's message holds) the next attempt
          // may use a new id; after a 5xx, a 429 or a reply
          // that isn't JSON the outcome is unknown, so the id stays until it succeeds.
          if (reply && error && res.status >= 400 && res.status < 500 && res.status !== 429) {
            idField.value = newId();
          }
        }
      }, function () {
        say(GENERIC, true);  // a network error: unknown outcome, the same id on retry
      })
      .then(finish, finish);
  });
}());
