// Public mode. Everything the visitor makes lives in this browser (localStorage). The server only computes.
(function () {
  "use strict";
  var KEY = "daylight.public.key", STATE = "daylight.public.v1";
  var $ = function (id) { return document.getElementById(id); };
  var state = load();
  var controller = null;

  function load() {
    try { return JSON.parse(localStorage.getItem(STATE)) || fresh(); } catch (e) { return fresh(); }
  }
  function fresh() { return { project: null, cards: [], feedback: [], seen: {}, excludedUrls: [], excludedAuthors: [], inbox: [], headline: "", costLine: "", note: "" }; }
  function save() { try { localStorage.setItem(STATE, JSON.stringify(state)); } catch (e) {} }
  function getKey() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }

  function el(tag, attrs) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) { var c = arguments[i]; if (c != null) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); }
    return n;
  }
  function link(url, text) { return el("a", { href: url, rel: "noopener noreferrer", target: "_blank", text: text || url }); }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function api(path, body, signal) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Gemini-Key": getKey() }, body: JSON.stringify(body || {}), signal: signal });
  }
  function nextId() { return "g_" + Math.random().toString(16).slice(2, 10); }

  // --- 1. key ----------------------------------------------------------------------------------
  function renderKey() {
    var has = !!getKey();
    $("key-form").hidden = has; $("key-delete").hidden = !has;
    $("key-state").textContent = has ? "A key is kept in this browser." : "";
    $("key-state").className = has ? "hint ok-text" : "hint";
    $("intake-panel").hidden = !has;
    renderAll();
  }
  $("key-save").addEventListener("click", function () {
    var v = $("key-input").value.trim();
    if (!v) return;
    $("key-state").textContent = "Checking the key. This is free.";
    fetch("/api/public/key-check", { method: "POST", headers: { "X-Gemini-Key": v } }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, body: j }; });
    }).then(function (res) {
      if (res.ok) { try { localStorage.setItem(KEY, v); } catch (e) {} $("key-input").value = ""; renderKey(); }
      else { $("key-state").textContent = res.body.error || "Key invalid."; $("key-state").className = "hint"; }
    }).catch(function () { $("key-state").textContent = "Could not reach the server. Try again."; });
  });
  $("key-delete").addEventListener("click", function () { try { localStorage.removeItem(KEY); } catch (e) {} renderKey(); });
  $("wipe").addEventListener("click", function () {
    if (!confirm("Delete the key, the project and every result from this browser?")) return;
    try { localStorage.removeItem(KEY); localStorage.removeItem(STATE); } catch (e) {}
    state = fresh(); renderKey();
  });

  // --- 2. intake -------------------------------------------------------------------------------
  $("intake-go").addEventListener("click", function () {
    var btn = $("intake-go"), msg = $("intake-state");
    btn.disabled = true; msg.textContent = "Reading your page and writing suggestions. Up to 30 seconds.";
    api("/api/public/intake", { url: $("p-url").value, goals: $("p-goals").value, description: $("p-desc").value }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, body: j }; });
    }).then(function (res) {
      btn.disabled = false;
      if (!res.ok) {
        msg.textContent = res.body.error || "That did not work.";
        if (res.body.need_description) { $("p-desc-wrap").hidden = false; msg.textContent += " Describe your project in one sentence and try again."; }
        return;
      }
      msg.textContent = res.body.proposal_error || "";
      state.project = { url: res.body.url, card: res.body.card, goals: res.body.goals };
      state.cards = []; state.headline = ""; state.costLine = ""; state.note = "";
      save(); renderAll();
    }).catch(function () { btn.disabled = false; msg.textContent = "Could not reach the server. Try again."; });
  });

  // --- 3. goals --------------------------------------------------------------------------------
  function active() { return (state.project ? state.project.goals : []).filter(function (g) { return g.status !== "removed"; }); }
  function renderGoals() {
    var has = !!state.project;
    $("goals-panel").hidden = !has; $("run-panel").hidden = !has;
    if (!has) return;
    var card = state.project.card || {}, box = $("project-card");
    clear(box);
    box.appendChild(el("h3", { text: card.name || state.project.url }));
    if (card.one_liner) box.appendChild(el("p", { class: "lede", text: card.one_liner }));
    if (card.audience) box.appendChild(el("p", {}, el("span", { class: "label", text: "Probably for " }), card.audience));
    var list = $("goal-list"); clear(list);
    state.project.goals.forEach(function (g) {
      var li = el("li", { class: "goal " + g.status });
      if (g.status === "removed") {
        li.appendChild(el("span", { class: "goal-text", text: g.text }));
        li.appendChild(el("span", { class: "muted", text: "Removed." }));
        li.appendChild(el("button", { type: "button", class: "link", text: "Undo", onclick: function () { g.status = "accepted"; save(); renderGoals(); } }));
      } else {
        var body = el("div", { class: "goal-body" }, el("p", { class: "goal-text" }, el("span", { class: "tag" + (g.origin === "user" ? " own" : ""), text: g.origin === "user" ? "Your goal" : "Suggestion" }), g.text));
        if (g.reason) body.appendChild(el("p", { class: "reason", text: "Reason: " + g.reason }));
        var acts = el("div", { class: "goal-actions" });
        if (g.status === "proposed") acts.appendChild(el("button", { type: "button", text: "Keep", onclick: function () { g.status = "accepted"; save(); renderGoals(); } }));
        acts.appendChild(el("button", { type: "button", class: "secondary", text: "Change", onclick: function () {
          var t = prompt("Change the goal", g.text); t = (t || "").replace(/\s+/g, " ").trim().slice(0, 200);
          if (t) { if (!g.original_text && t !== g.text) g.original_text = g.text; g.text = t; g.status = "edited"; save(); renderGoals(); }
        } }));
        acts.appendChild(el("button", { type: "button", class: "secondary", text: "Remove", onclick: function () { g.status = "removed"; save(); renderGoals(); } }));
        li.appendChild(body); li.appendChild(acts);
      }
      list.appendChild(li);
    });
    $("run-go").disabled = active().length === 0 || !!controller;
  }
  $("goal-add").addEventListener("click", function () {
    var t = $("goal-new").value.replace(/\s+/g, " ").trim().slice(0, 200);
    if (!t || !state.project || active().length >= 20) return;
    state.project.goals.push({ id: nextId(), text: t, origin: "user", reason: null, status: "accepted" });
    $("goal-new").value = ""; save(); renderGoals();
  });

  // --- 4. run ----------------------------------------------------------------------------------
  var tasks = {};
  function renderCrew() {
    var ol = $("crew"); clear(ol);
    Object.keys(tasks).forEach(function (id) {
      var t = tasks[id];
      var li = el("li", { class: "task " + t.status });
      li.appendChild(el("p", {}, el("span", { class: "chip " + t.status, text: t.status }), " ", el("strong", { text: t.role || id }), " ", el("span", { class: "muted small", text: (t.found || 0) + " found" })));
      if (t.rationale) li.appendChild(el("p", { class: "reason", text: t.rationale }));
      if (t.instruction) li.appendChild(el("details", {}, el("summary", { text: "Its instruction" }), el("p", { class: "small", text: t.instruction })));
      if (t.reason) li.appendChild(el("p", { class: "small", text: t.reason }));
      (t.replaced || []).forEach(function (h) {
        li.appendChild(el("p", { class: "replaced small" }, el("strong", { text: "Replaced. " }), h.reason || "", h.new_instruction ? el("span", { class: "muted", text: " New instruction: " + h.new_instruction }) : null));
      });
      ol.appendChild(li);
    });
  }
  function onEvent(ev) {
    if (ev.type === "plan") {
      tasks = {}; (ev.tasks || []).forEach(function (t) { tasks[t.id] = { role: t.role, rationale: t.rationale, instruction: t.instruction, status: t.status, found: t.found, replaced: [] }; });
      state.note = ev.feedback_note || ""; renderNote(); renderCrew();
    } else if (ev.type === "task") {
      var t = tasks[ev.id] || (tasks[ev.id] = { status: "pending", replaced: [] });
      t.status = ev.status; if (ev.found != null) t.found = ev.found; if (ev.reason) t.reason = ev.reason; renderCrew();
    } else if (ev.type === "replace") {
      var r = tasks[ev.id] || (tasks[ev.id] = { status: "pending", replaced: [] });
      r.status = "replacing"; r.replaced.push({ reason: ev.reason, new_instruction: ev.new_instruction }); renderCrew();
    } else if (ev.type === "card") {
      state.cards = state.cards.filter(function (c) { return c.id !== ev.card.id; }).concat([ev.card]).sort(function (a, b) { return a.rank - b.rank; });
      save(); renderCards();
    } else if (ev.type === "done") {
      state.headline = ev.headline || ""; state.costLine = ev.cost_line || "";
      var ok = ev.ledger && (ev.ledger.status === "ok" || ev.ledger.status === "partial");
      state.cards.forEach(function (c) { state.seen[c.url] = (c.date || "").slice(0, 10) || "seen"; });
      if (ok) state.feedback = [];
      save(); renderRunState(); renderCards();
    } else if (ev.type === "error") { $("run-state").textContent = ev.message || "The run ended unexpectedly."; }
  }
  function renderNote() { var n = $("feedback-note"); n.hidden = !state.note; clear(n); if (state.note) n.appendChild(el("span", {}, el("strong", { text: "Changed because of your feedback: " }), state.note)); }
  function renderRunState() {
    $("run-state").textContent = controller ? "The crew is at work. Keep this tab open." : [state.headline, state.costLine].filter(Boolean).join(" ");
    $("run-go").hidden = !!controller; $("run-stop").hidden = !controller; $("run-go").disabled = active().length === 0;
  }
  $("run-stop").addEventListener("click", function () { if (controller) controller.abort(); });
  $("run-go").addEventListener("click", function () {
    var p = state.project; if (!p || !active().length) return;
    var budget = parseFloat(($("run-budget").value || "").replace(",", "."));
    var payload = { project: {
      name: (p.card && p.card.name) || p.url, url: p.url, one_liner: (p.card && p.card.one_liner) || "", audience: (p.card && p.card.audience) || "",
      goals: active().map(function (g) { return g.text; }), feedback: state.feedback.slice(-20),
      seen_urls: Object.keys(state.seen).slice(-200), excluded_urls: state.excludedUrls.slice(-100), excluded_authors: state.excludedAuthors.slice(-100)
    } };
    if (isFinite(budget) && budget >= 0) payload.budget = budget;
    tasks = {}; state.cards = []; state.headline = ""; state.costLine = ""; state.note = ""; save(); renderCrew(); renderCards(); renderNote();
    controller = new AbortController(); renderRunState();
    api("/api/public/run", payload, controller.signal).then(function (resp) {
      if (!resp.ok) return resp.json().then(function (j) { throw new Error(j.error || "The run could not start."); });
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          buf += dec.decode(r.value, { stream: true });
          var parts = buf.split("\n\n"); buf = parts.pop();
          parts.forEach(function (chunk) {
            chunk.split("\n").forEach(function (line) { if (line.indexOf("data: ") === 0) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) {} } });
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      $("run-state").textContent = err.name === "AbortError" ? "Stopped. What the crew had found is kept below." : (err.message || "The run failed.");
    }).then(function () { controller = null; renderRunState(); renderCards(); renderGoals(); });
  });

  // --- cards -----------------------------------------------------------------------------------
  function copyText(t) { if (navigator.clipboard) navigator.clipboard.writeText(t).catch(function () {}); }
  function feedback(c, kind, extra) {
    var fb = Object.assign({ kind: kind, comment: "", card_title: c.title, card_url: c.url, original: "", final: "" }, extra || {});
    state.feedback = state.feedback.filter(function (f) { return !(f.card_url === c.url && (f.kind === "edit") === (kind === "edit")); }).concat([fb]);
    if (kind === "down") {
      if (state.excludedUrls.indexOf(c.url) < 0) state.excludedUrls.push(c.url);
      var a = (c.author || "").toLowerCase().trim(); if (a && state.excludedAuthors.indexOf(a) < 0) state.excludedAuthors.push(a);
    }
    save();
  }
  function renderCards() {
    var box = $("results"); clear(box);
    if (state.headline) box.appendChild(el("p", { class: "notice", role: "status", text: state.headline }));
    state.cards.forEach(function (c) {
      var art = el("article", { class: "opening " + c.kind, id: c.id });
      art.appendChild(el("p", { class: "eyebrow", text: c.kind === "question" ? "A question you can answer" : "A writer who covers your topic" }));
      art.appendChild(el("h2", {}, link(c.url, c.title)));
      art.appendChild(el("p", { class: "meta muted small" }, (c.date || "") + (c.date_basis === "text" ? " (date found in the page text)" : "") + (c.author ? " · " + c.author : "") + " · ", link(c.url, "Open source")));
      art.appendChild(el("p", { class: "why", text: c.why }));
      art.appendChild(el("blockquote", { text: c.quote }));
      if (c.kind === "resonance") art.appendChild(c.contact_route ? el("p", { class: "small" }, el("span", { class: "label", text: "Contact route from their own page: " }), c.contact_route + " ", link(c.contact_source_url, "where it is shown")) : el("p", { class: "small muted", text: "Their page shows no contact route. Use the source link." }));
      if (c.needs_attention && c.needs_attention.length) art.appendChild(el("p", { class: "notice small", text: "This draft needs a look: " + c.needs_attention.join("; ") + "." }));
      var draft = el("pre", { class: "draft", text: c.draft }); art.appendChild(el("span", { class: "label", text: "Draft" })); art.appendChild(draft);
      var acts = el("div", { class: "card-actions" });
      if (c.state === "signed") {
        acts.appendChild(el("span", { class: "kept", text: "Signed. It is in your inbox." }));
        acts.appendChild(el("button", { type: "button", class: "secondary", text: "Copy again", onclick: function () { copyText(c.draft); } }));
        acts.appendChild(el("button", { type: "button", class: "link", text: "Undo", onclick: function () { c.state = "new"; state.inbox = state.inbox.filter(function (i) { return i.url !== c.url; }); save(); renderCards(); } }));
      } else {
        acts.appendChild(el("button", { type: "button", text: "Sign and copy", onclick: function () { copyText(c.draft); c.state = "signed"; c.signed_at = new Date().toISOString(); state.inbox = state.inbox.filter(function (i) { return i.url !== c.url; }).concat([c]); save(); renderCards(); } }));
      }
      art.appendChild(acts);
      var ta = el("textarea", { rows: "9", maxlength: "2000" }); ta.value = c.draft;
      art.appendChild(el("details", { class: "edit" }, el("summary", { text: "Change the draft" }), ta, el("button", { type: "button", class: "secondary", text: "Save my version", onclick: function () {
        var t = ta.value.trim().slice(0, 2000); if (!t || t === c.draft) return;
        feedback(c, "edit", { original: c.draft.slice(0, 900), final: t.slice(0, 900) }); c.draft = t; c.edited = true; save(); renderCards();
      } })));
      var cm = el("input", { maxlength: "500", placeholder: "Why? One sentence helps the next night.", "aria-label": "Comment (optional)" }); cm.value = c.comment || "";
      function thumb(kind) { return el("button", { type: "button", class: "secondary" + (c.thumb === kind ? " picked" : ""), text: kind === "up" ? "Useful" : "Not useful", onclick: function () { c.thumb = kind; c.comment = cm.value.trim().slice(0, 500); feedback(c, kind, { comment: c.comment }); renderCards(); } }); }
      art.appendChild(el("div", { class: "thumbs" }, cm, thumb("up"), thumb("down")));
      box.appendChild(art);
    });
    renderInbox();
  }
  function renderInbox() {
    var box = $("inbox"); clear(box); $("inbox-panel").hidden = state.inbox.length === 0;
    state.inbox.slice().reverse().forEach(function (c) {
      var pre = el("pre", { class: "draft", text: c.draft });
      box.appendChild(el("article", { class: "opening" }, el("p", { class: "eyebrow", text: (c.kind === "question" ? "Question" : "Writer") + " · signed " + (c.signed_at || "").slice(0, 10) }), el("h3", {}, link(c.url, c.title)), c.contact_route ? el("p", { class: "small", text: "Contact route: " + c.contact_route }) : null, pre, el("button", { type: "button", class: "secondary", text: "Copy", onclick: function () { copyText(c.draft); } })));
    });
  }
  function renderAll() { renderGoals(); renderNote(); renderRunState(); renderCards(); }
  renderKey();
})();
