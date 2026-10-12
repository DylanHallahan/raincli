(function () {
  "use strict";
  function tick() { window.rcStatus.refresh().catch(function () {}); }
  tick();
  setInterval(tick, 5000);

  // Coding agents (protocol §16.19 item 3): each agent's state, and Connect or Disconnect on the user's click.
  var STATES = {
    not_installed_agent: "Not installed on this computer",
    too_old: "Installed, but too old to connect: update it first",
    not_connected: "Not connected",
    connected: "Connected",
    needs_approval: "Connected: approve RainCLI's hooks once in the Codex CLI's /hooks",
    unknown: "Couldn't check"
  };
  function say(text, isError) {
    var el = document.getElementById("hooks-message");
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }
  function render(agents) {
    var list = document.getElementById("hooks");
    list.textContent = "";
    (agents || []).forEach(function (agent) {
      var li = document.createElement("li");
      li.setAttribute("data-agent", agent.kind);
      var label = document.createElement("span");
      var name = document.createElement("strong");
      name.textContent = agent.name;
      var state = document.createElement("span");
      state.className = "muted small";
      state.setAttribute("data-state", agent.state);
      state.textContent = " " + (STATES[agent.state] || STATES.unknown);
      label.appendChild(name); label.appendChild(state); li.appendChild(label);
      var connect = agent.state === "not_connected";
      var disconnect = agent.state === "connected" || agent.state === "needs_approval";
      if (connect || disconnect) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = connect ? "btn" : "btn btn-quiet";
        button.textContent = connect ? "Connect" : "Disconnect";
        button.setAttribute("data-connect", connect ? "1" : "0");
        button.addEventListener("click", function () {
          button.disabled = true;
          say(connect ? "Connecting " + agent.name + "…" : "Disconnecting " + agent.name + "…");
          rc.call("connect_hooks", agent.kind, connect).then(function (r) {
            say(r.message || (r.ok ? "" : "Failed."), !r.ok);
            load(true);
          }, function () { say("That didn't work. Try again.", true); load(true); });
        });
        li.appendChild(button);
      }
      list.appendChild(li);
    });
  }
  function load(keepMessage) {
    return rc.read("hooks").then(render, function () {
      if (!keepMessage) say("Couldn't check the coding agents.", true);
    });
  }
  load();

  // Your inbox (protocol §21 C9.1): this computer's agents only (C13 L4), "Use as my inbox", Change, and the
  // waiting messages' tick (on by default). Every change is the user's click.
  var inboxOpen = false;
  function inboxSay(text, isError) {
    var el = document.getElementById("inbox-message");
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }
  function renderInbox(r) {
    if (!r || r.ok === false) {
      rc.text("inbox-status", "Couldn't read your inbox");
      if (r && r.message) inboxSay(r.message, true);
      return;
    }
    rc.text("inbox-status", r.status);
    var picking = !r.inbox || inboxOpen;
    rc.show("inbox-change", !!r.inbox);
    var change = document.getElementById("inbox-change");
    change.textContent = inboxOpen ? "Cancel" : "Change";
    change.setAttribute("aria-expanded", picking ? "true" : "false");
    rc.show("inbox-picker", picking);
    rc.show("inbox-stayed", !!r.stayed_label);
    rc.text("inbox-stayed-text", r.stayed_label || "");
    rc.show("inbox-waiting-row", !!r.waiting_label);
    rc.text("inbox-waiting-label", r.waiting_label || "");
    rc.show("inbox-clear-row", !!r.inbox && inboxOpen);
    var list = document.getElementById("inbox-candidates");
    list.textContent = "";
    var candidates = r.candidates || [];
    rc.show("inbox-none", !candidates.length);
    candidates.forEach(function (c) {
      var li = document.createElement("li");
      li.setAttribute("data-inbox-agent", c.agent);
      var label = document.createElement("span");
      var dot = document.createElement("span");
      dot.className = "dot" + (c.live ? " ok" : "");
      dot.setAttribute("aria-hidden", "true");
      label.appendChild(dot);
      label.appendChild(document.createTextNode(c.agent));
      if (c.suggested && !c.current) {
        var hint = document.createElement("span");
        hint.className = "muted small";
        hint.textContent = " suggested";
        label.appendChild(hint);
      }
      li.appendChild(label);
      if (c.current) {
        var pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = "Your inbox";
        li.appendChild(pill);
      } else {
        var button = document.createElement("button");
        button.type = "button";
        button.className = c.suggested ? "btn" : "btn btn-quiet";
        button.textContent = r.use_label;
        button.addEventListener("click", function () {
          button.disabled = true;
          var waiting = document.getElementById("inbox-waiting");
          var deliver = !r.waiting_label || waiting.checked;
          inboxSay("Setting your inbox…");
          rc.call("set_inbox", c.agent, deliver).then(function (res) {
            inboxSay(res.message || (res.ok ? "" : "Failed."), !res.ok);
            if (res.ok) inboxOpen = false;
            loadInbox();
          }, function () { inboxSay("That didn't work. Try again.", true); loadInbox(); });
        });
        li.appendChild(button);
      }
      list.appendChild(li);
    });
  }
  function loadInbox() {
    return rc.read("inbox").then(renderInbox, function () {
      rc.text("inbox-status", "Couldn't read your inbox");
    });
  }
  document.getElementById("inbox-change").addEventListener("click", function () {
    inboxOpen = !inboxOpen;
    inboxSay("");
    loadInbox();
  });
  document.getElementById("inbox-stayed-dismiss").addEventListener("click", function () {
    rc.call("dismiss_inbox_notice").then(loadInbox, loadInbox);
  });
  document.getElementById("inbox-clear").addEventListener("click", function () {
    var button = document.getElementById("inbox-clear");
    button.disabled = true;
    rc.call("clear_inbox").then(function (res) {
      button.disabled = false;
      inboxSay(res.message, !res.ok);
      if (res.ok) inboxOpen = false;
      loadInbox();
    }, function () { button.disabled = false; inboxSay("That didn't work. Try again.", true); });
  });
  loadInbox();
}());
