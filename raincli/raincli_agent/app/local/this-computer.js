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
    return rc.call("hooks").then(render, function () {
      if (!keepMessage) say("Couldn't check the coding agents.", true);
    });
  }
  load();
}());
