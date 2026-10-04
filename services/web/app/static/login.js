(function () {
  var root = document.getElementById("login");
  if (!root) return;
  var msg = document.getElementById("login-message");
  function fail(text) { msg.textContent = text; msg.hidden = false; }
  function finish(idToken) {
    return fetch("/auth/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id_token: idToken }) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
      .then(function (res) {
        if (res.ok) { location.href = "/app"; return; }
        fail(res.status === 403 ? "This account is not on the list. Ask the person who runs this app to add it." : "Sign-in failed. Try again.");
      })
      .catch(function () { fail("Sign-in failed. Check your connection and try again."); });
  }
  if (root.dataset.dev === "yes") {
    document.getElementById("dev-form").addEventListener("submit", function (e) {
      e.preventDefault();
      finish("dev:" + document.getElementById("dev-email").value.trim());
    });
    return;
  }
  var btn = document.getElementById("google-signin");
  btn.addEventListener("click", function () {
    btn.disabled = true;
    import("/static/firebase-auth.js").then(function (mod) {
      return mod.signIn(JSON.parse(root.dataset.config));
    }).then(finish).catch(function () { btn.disabled = false; fail("Sign-in was cancelled or failed."); });
  });
})();
