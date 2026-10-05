// Sign-in (protocol §15.2, §16.3): the password goes only to the app's sign_in, which calls login.login.
// After any refusal the password field is cleared; the app never retries with it (§15.8 M2).
(function () {
  "use strict";
  var form = document.getElementById("sign-in");
  var again = /[?&]again=1\b/.test(location.search);
  rc.call("sign_in_defaults").then(function (d) {
    document.getElementById("machine").value = d.machine_name || "";
    if (d.email) document.getElementById("email").value = d.email;
    if (again) rc.text("intro", "Sign in again to replace this computer's credential. Its name and agents stay the same.");
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var password = document.getElementById("password");
    var secret = password.value;
    password.value = "";
    var team = document.getElementById("team");
    var request = {
      email: document.getElementById("email").value.trim(),
      machine_name: document.getElementById("machine").value.trim(),
      team: document.getElementById("team-field").classList.contains("hidden") ? null : team.value,
      replace: document.getElementById("replace").checked,
      again: again
    };
    if (!secret) { rc.message("Enter your password.", true); return; }
    var button = document.getElementById("submit");
    button.disabled = true;
    rc.message("Signing in…");
    rc.call("sign_in", request, secret).then(function (result) {
      secret = null;
      button.disabled = false;
      if (result.ok) { rc.message("Signed in. Opening your inbox…"); return; }
      rc.message(result.message, true);
      if (result.teams && result.teams.length) {
        team.textContent = "";
        result.teams.forEach(function (t) {
          var option = document.createElement("option");
          option.value = t.slug; option.textContent = t.name;
          team.appendChild(option);
        });
        rc.show("team-field", true);
      }
      if (result.code === "name_in_use") {
        rc.text("replace-label", "Replace machine " + request.machine_name + " (its current credential stops working)");
        rc.show("replace-field", true);
      }
      password.focus();
    }, function () { secret = null; button.disabled = false; rc.message("Signing in failed. Try again.", true); });
  });
}());
