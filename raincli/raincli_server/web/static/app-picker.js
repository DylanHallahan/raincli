// The people picker on New message (protocol §21 C7.2, from §20.1 and §20.6 L1-L3), app mode only. It replaces the
// page's people <select> with an ARIA combobox: type to filter by name or email (the email is a hidden match term,
// never displayed), arrow keys, Enter to pick, Esc to close; the pick shows as a chip with a remove button. People
// only: no machines, agents or handles. Recent people first. The list is fetched when the picker opens and at most
// every 30 s while it stays open; presence older than 120 s on this side reads Offline. Without JavaScript (or
// fetch) the <select> stays and works as it is.
(function () {
  "use strict";
  var box = document.querySelector("[data-picker]");
  var select = box && box.querySelector("select#to");
  if (!box || !select || !window.fetch) return;
  var url = box.getAttribute("data-recipients-url");
  var form = select.form;
  var STALE = 120000, people = [], fetchedAt = 0, refresh = 30000, active = -1, shown = [];

  var hidden = document.createElement("input");
  hidden.type = "hidden";
  hidden.name = "to";
  hidden.value = select.value;
  var input = document.createElement("input");
  input.type = "text";
  input.id = "to";
  input.className = "rc-picker-input";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", "to-options");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("spellcheck", "false");
  input.placeholder = "Type a name";
  var list = document.createElement("ul");
  list.id = "to-options";
  list.className = "rc-picker-list";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "People");
  list.hidden = true;
  var chip = document.createElement("span");
  chip.className = "rc-chip-picked";
  chip.hidden = true;
  var status = document.createElement("span");
  status.className = "sr-only";
  status.setAttribute("role", "status");

  // The <select> leaves the form; its label now names the combobox (same id).
  var initial = select.options[select.selectedIndex];
  select.removeAttribute("id");
  select.removeAttribute("name");
  select.disabled = true;
  select.hidden = true;
  box.appendChild(hidden);
  box.appendChild(chip);
  box.appendChild(input);
  box.appendChild(list);
  box.appendChild(status);

  function presence(p) { return Date.now() - fetchedAt > STALE ? "Offline" : p.status; }
  function spoken(p) { return p.name + ", " + presence(p) + (p.team ? ", " + p.team : ""); }

  function load() {
    if (Date.now() - fetchedAt < refresh && people.length) return Promise.resolve();
    return fetch(url, {credentials: "same-origin", cache: "no-store", headers: {"Accept": "application/json"}})
      .then(function (r) { if (!r.ok) throw new Error("status " + r.status); return r.json(); })
      .then(function (data) {
        people = data.recipients || [];
        refresh = (Number(data.refresh) || 30) * 1000;
        fetchedAt = Date.now();
      });
  }

  function matches(p, q) {
    if (!q) return 0;
    var name = p.name.toLowerCase();
    if (name.indexOf(q) === 0) return 1;
    if (name.indexOf(q) >= 0 || (p.team || "").toLowerCase().indexOf(q) >= 0) return 2;
    return p.match.some(function (t) { return t.indexOf(q) >= 0; }) ? 3 : -1;
  }

  function render() {
    var q = input.value.trim().toLowerCase();
    shown = people.map(function (p) { return {p: p, rank: q ? matches(p, q) : (p.group === "recent" ? 0 : 1)}; })
      .filter(function (x) { return x.rank >= 0; })
      .sort(function (a, b) { return a.rank - b.rank; })
      .map(function (x) { return x.p; });
    while (list.firstChild) list.removeChild(list.firstChild);
    var group = null;
    shown.forEach(function (p, i) {
      var heading = q ? null : (p.group === "recent" ? "Recent" : "Everyone");
      if (heading && heading !== group) {
        group = heading;
        var h = document.createElement("li");
        h.className = "rc-picker-group";
        h.setAttribute("role", "presentation");
        h.textContent = heading;
        list.appendChild(h);
      }
      var li = document.createElement("li");
      li.id = "to-option-" + i;
      li.className = "rc-picker-option";
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      li.setAttribute("aria-label", spoken(p));
      var name = document.createElement("span");
      name.className = "rc-picker-name";
      name.textContent = p.name;
      var meta = document.createElement("span");
      meta.className = "rc-picker-meta";
      meta.textContent = presence(p) + (p.team ? " · " + p.team : "");
      li.appendChild(name);
      li.appendChild(meta);
      li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(p); });
      list.appendChild(li);
    });
    if (!shown.length) {
      var none = document.createElement("li");
      none.className = "rc-picker-none";
      none.setAttribute("role", "presentation");
      none.textContent = q ? "Nobody on your team matches '" + input.value.trim() + "'" : "No teammates yet";
      list.appendChild(none);
    }
    if (active >= 0 && shown[active]) input.setAttribute("aria-activedescendant", "to-option-" + active);
    else input.removeAttribute("aria-activedescendant");
    status.textContent = shown.length ? shown.length + " people" : "";
  }

  function open() {
    if (!list.hidden) return;
    load().then(function () {
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
      render();
    }, function () { /* the list stays closed; the field still takes a typed email or name */ });
  }
  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }
  function move(step) {
    if (list.hidden) { open(); return; }
    if (!shown.length) return;
    active = (active + step + shown.length) % shown.length;
    render();
    var el = document.getElementById("to-option-" + active);
    if (el && el.scrollIntoView) el.scrollIntoView({block: "nearest"});
  }
  function pick(p) {
    hidden.value = p.id;
    while (chip.firstChild) chip.removeChild(chip.firstChild);
    var text = document.createElement("span");
    text.textContent = p.name;
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "rc-chip-remove";
    remove.setAttribute("aria-label", "Remove " + p.name);
    remove.textContent = "×";
    remove.addEventListener("click", unpick);
    chip.appendChild(text);
    chip.appendChild(remove);
    chip.hidden = false;
    input.hidden = true;
    input.value = "";
    close();
    status.textContent = "To " + spoken(p);
    form.dispatchEvent(new CustomEvent("rc-recipient-picked"));
    var body = form.querySelector("textarea#body");
    if (body) body.focus({preventScroll: true});
  }
  function unpick() {
    hidden.value = "";
    chip.hidden = true;
    input.hidden = false;
    input.focus({preventScroll: true});
    form.dispatchEvent(new CustomEvent("rc-recipient-picked"));
  }

  input.addEventListener("focus", open);
  input.addEventListener("click", open);
  input.addEventListener("input", function () { if (list.hidden) open(); else { active = shown.length ? 0 : -1; render(); } });
  input.addEventListener("blur", function () { setTimeout(close, 100); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter") {
      if (!list.hidden && shown[active >= 0 ? active : 0] && (active >= 0 || input.value.trim())) {
        e.preventDefault();
        pick(shown[active >= 0 ? active : 0]);
      } else if (!hidden.value) {
        e.preventDefault();  // nobody picked yet: Enter never sends to a half-typed name
      }
    } else if (e.key === "Escape") {
      if (!list.hidden) { e.preventDefault(); close(); }
    }
  });
  // While the list is open, refresh it at most every 30 s (§20.6 L3).
  setInterval(function () { if (!list.hidden) load().then(render, function () {}); }, 30000);
  // A recipient the server pre-picked (?to=): a chip at once, from the <select>'s own option.
  if (initial && initial.value) {
    load().then(function () {
      var p = people.filter(function (x) { return x.id === initial.value; })[0];
      if (p) pick(p);
    }, function () {});
  }
  form.addEventListener("submit", function (e) {
    if (!hidden.value) { e.preventDefault(); input.hidden = false; input.focus({preventScroll: true}); open(); }
  });
}());
