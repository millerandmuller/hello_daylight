// Sign copies the draft to the clipboard first, then the form posts. Nothing is sent anywhere else.
(function () {
  function copy(id) {
    var node = document.getElementById(id);
    if (!node || !navigator.clipboard) return Promise.resolve();
    return navigator.clipboard.writeText(node.textContent.trim()).catch(function () {});
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-from]");
    if (!btn) return;
    copy(btn.getAttribute("data-copy-from"));
    if (btn.type === "button") {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1500);
    }
  });
})();
