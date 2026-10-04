(function () {
  var form = document.getElementById("intake-form");
  if (!form) return;
  form.addEventListener("submit", function () {
    var b = document.getElementById("intake-submit");
    b.disabled = true; b.textContent = "Reading…";
    document.getElementById("intake-wait").hidden = false;
    setTimeout(function () { document.getElementById("intake-step").textContent = "Writing suggestions."; }, 4000);
  });
})();
