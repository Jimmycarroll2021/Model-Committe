"use strict";

(function () {
  const STAGES = [
    { id: "research", label: "Research" },
    { id: "synthesis", label: "Synthesis" },
    { id: "validation", label: "Validation" },
    { id: "outputs", label: "Outputs" },
  ];
  const STATUS_TEXT = {
    pending: "Pending", running: "Running", succeeded: "Succeeded", failed: "Failed",
    timed_out: "Timed out", skipped: "Skipped", cancelled: "Cancelled", interrupted: "Interrupted",
  };
  const STAGE_TEXT = { pending: "Waiting", running: "In progress", done: "Done", failed: "Failed", skipped: "Skipped" };

  const state = { meta: null, rows: [], run: null, es: null, ticker: null, nextRowId: 0, context: [] };

  // ------------------------------------------------------------------ helpers

  const $ = (id) => document.getElementById(id);

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  // Access token (only needed when the server was started with MODEL_COMMITTEE_TOKEN): taken once from the
  // URL fragment (#token=...), kept in sessionStorage, sent as a bearer header. The server answers with an
  // HttpOnly cookie so EventSource and artifact links (which cannot send headers) are authenticated too.
  const TOKEN_KEY = "model-committee-token";
  let accessToken = "";

  function initToken() {
    const params = new URLSearchParams(location.hash.slice(1));
    try {
      if (params.get("token")) sessionStorage.setItem(TOKEN_KEY, params.get("token"));
      accessToken = sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (_) {
      accessToken = params.get("token") || "";
    }
    if (params.has("token")) {
      params.delete("token");
      const rest = params.toString();
      history.replaceState(null, "", location.pathname + location.search + (rest ? "#" + rest : ""));
    }
    return params;
  }

  function authFetch(path, opts) {
    const o = Object.assign({}, opts || {});
    o.headers = Object.assign({}, o.headers || {});
    if (accessToken) o.headers.Authorization = "Bearer " + accessToken;
    return fetch(path, o);
  }

  async function api(path, opts) {
    const resp = await authFetch(path, opts);
    let body = null;
    try { body = await resp.json(); } catch (_) { body = null; }
    if (!resp.ok) {
      const err = new Error(describeError(body, resp.status));
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  function describeError(body, status) {
    if (body && Array.isArray(body.detail)) {
      return body.detail.map((d) => {
        const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return (where ? where + ": " : "") + String(d.msg || "").replace(/^Value error, /, "");
      }).join("\n");
    }
    if (status === 401) return "This server needs an access token. Open it with #token=<your token> at the end of the address.";
    if (body && typeof body.detail === "string") return body.detail;
    return "Request failed (HTTP " + status + ")";
  }

  function fmtElapsed(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "";
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    if (h > 0) return h + "h " + String(m % 60).padStart(2, "0") + "m";
    return m + ":" + String(s % 60).padStart(2, "0");
  }

  function parseTs(ts) {
    if (!ts) return null;
    const t = Date.parse(ts);
    return Number.isNaN(t) ? null : t;
  }

  function fmtDate(ts) {
    const t = parseTs(ts);
    if (t === null) return "";
    return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  // A failed run whose process died (server or CLI exited) is "interrupted", one the user stopped is
  // "cancelled"; both arrive as status "failed" with a telling error.
  function displayStatus(status, error) {
    if (status !== "failed" || typeof error !== "string") return status;
    if (error.startsWith("interrupted")) return "interrupted";
    if (error === "cancelled by user" || error === "cancelled") return "cancelled";
    return status;
  }

  function pill(status) {
    const s = status || "pending";
    return el("span", { class: "pill s-" + s, text: STATUS_TEXT[s] || s });
  }

  function fileUrl(runId, path, download) {
    const enc = path.split("/").map(encodeURIComponent).join("/");
    return "/api/runs/" + encodeURIComponent(runId) + "/files/" + enc + (download ? "?download=1" : "");
  }

  function announce(text) {
    const node = $("announcer");
    node.textContent = "";
    window.setTimeout(() => { node.textContent = text; }, 30);
  }

  // ------------------------------------------------------------------ model picker

  function providerFor(spec) {
    if (!state.meta) return null;
    const s = (spec || "").trim();
    return state.meta.providers.find((p) => s === p.name || s.startsWith(p.name + ":")) || null;
  }

  function specsFor(p) {
    const out = [];
    if (p.example_spec) out.push(p.example_spec);
    for (const m of p.models || []) {
      out.push(m === p.name || m.startsWith(p.name + ":") ? m : p.name + ":" + m);
    }
    return out;
  }

  function buildRows(meta) {
    const rows = [];
    const lineup = meta.default_lineup || [];
    for (const spec of lineup) rows.push({ spec, checked: true, custom: false });
    for (const p of meta.providers) {
      const represented = rows.some((r) => providerFor(r.spec) === p);
      if (!represented) rows.push({ spec: p.example_spec || p.name, checked: false, custom: false });
    }
    return rows.map((r) => Object.assign({ id: state.nextRowId++ }, r));
  }

  function renderModelRows() {
    const list = $("model-list");
    list.replaceChildren();
    for (const row of state.rows) {
      const p = providerFor(row.spec);
      const unavailable = p !== null && p.available === false;
      const cbId = "model-cb-" + row.id;
      const specId = "model-spec-" + row.id;
      const reasonId = "model-reason-" + row.id;
      const checkbox = el("input", {
        type: "checkbox", id: cbId, checked: row.checked && !unavailable, disabled: unavailable,
        "aria-describedby": reasonId,
        onchange: (e) => { row.checked = e.target.checked; li.classList.toggle("on", row.checked); updateCount(); },
      });
      const vendor = p ? p.vendor || "" : "custom";
      const who = el("label", { for: cbId, class: "who" }, el("strong", { text: p ? p.name : "Custom model" }),
        vendor ? " · " + vendor : "");
      const spec = el("input", {
        type: "text", id: specId, value: row.spec, list: "spec-options", spellcheck: "false",
        autocomplete: "off", "aria-label": "Model spec" + (p ? " for " + p.name : ""), disabled: unavailable,
        oninput: (e) => { row.spec = e.target.value; updateCount(); },
        onchange: () => { renderModelRows(); updateCount(); },
      });
      let reasonText = "";
      if (unavailable) reasonText = "Unavailable: " + (p.reason || "not detected on this machine");
      else if (p === null && row.spec.trim()) reasonText = "Unknown provider prefix; the run will fail if it cannot be resolved.";
      else if (p && p.reason && p.reason !== "ok") reasonText = p.reason;
      const reason = el("p", { class: "reason", id: reasonId, text: reasonText });
      const remove = row.custom
        ? el("button", {
          type: "button", class: "icon-btn remove", "aria-label": "Remove " + (row.spec || "model"), text: "×",
          onclick: () => { state.rows = state.rows.filter((r) => r !== row); renderModelRows(); updateCount(); },
        })
        : null;
      const li = el("li", { class: "model-row" + (row.checked && !unavailable ? " on" : "") + (unavailable ? " off-limits" : "") },
        checkbox, who, remove, spec, reasonText ? reason : null);
      list.append(li);
    }
    updateCount();
  }

  function selectedModels() {
    return state.rows
      .filter((r) => r.checked && r.spec.trim() && !(providerFor(r.spec) && providerFor(r.spec).available === false))
      .map((r) => r.spec.trim());
  }

  function updateCount() {
    const meta = state.meta || { min_members: 2, max_members: 8 };
    const n = selectedModels().length;
    const node = $("model-count");
    const ok = n >= meta.min_members && n <= meta.max_members;
    node.textContent = n + " selected" + (ok ? "" : " (need " + meta.min_members + " to " + meta.max_members + ")");
    node.classList.toggle("bad", !ok);
    $("add-model").disabled = state.rows.length >= 16;
    return ok;
  }

  function fillSynthesizer(meta) {
    const select = $("synthesizer");
    const seen = new Set();
    const options = [meta.default_synthesizer || "claude-cli", ...(meta.default_lineup || [])];
    for (const p of meta.providers) if (p.available !== false) options.push(...specsFor(p));
    select.replaceChildren();
    for (const spec of options) {
      if (!spec || seen.has(spec)) continue;
      seen.add(spec);
      select.append(el("option", { value: spec, text: spec }));
    }
    select.value = meta.default_synthesizer || options[0];

    const dl = $("spec-options");
    dl.replaceChildren();
    const all = new Set(meta.default_lineup || []);
    for (const p of meta.providers) for (const s of specsFor(p)) all.add(s);
    for (const s of all) dl.append(el("option", { value: s }));
  }

  async function loadProviders() {
    try {
      state.meta = await api("/api/providers");
      state.rows = buildRows(state.meta);
      fillSynthesizer(state.meta);
    } catch (err) {
      state.meta = { providers: [], default_lineup: [], default_synthesizer: "claude-cli", min_members: 2, max_members: 8 };
      state.rows = [
        { id: state.nextRowId++, spec: "", checked: true, custom: true },
        { id: state.nextRowId++, spec: "", checked: true, custom: true },
      ];
      fillSynthesizer(state.meta);
      $("models-hint").textContent = "Could not detect providers (" + err.message + "). Type model specs by hand.";
    }
    renderModelRows();
  }

  // ------------------------------------------------------------------ form

  function showFormError(msg) {
    const box = $("form-error");
    box.textContent = msg;
    box.hidden = !msg;
  }

  async function onSubmit(e) {
    e.preventDefault();
    showFormError("");
    const objective = $("objective").value.trim();
    $("objective").setAttribute("aria-invalid", objective.length < 3 ? "true" : "false");
    if (objective.length < 3) {
      showFormError("Write a question or objective (at least 3 characters).");
      $("objective").focus();
      return;
    }
    if (!updateCount()) {
      showFormError("Select between " + state.meta.min_members + " and " + state.meta.max_members + " models.");
      $("model-list").querySelector("input")?.focus();
      return;
    }
    const payload = {
      objective,
      models: selectedModels(),
      synthesizer: $("synthesizer").value,
      depth: document.querySelector("input[name=depth]:checked").value,
      deliverable: $("deliverable").value,
      criteria: $("criteria").value.split("\n").map((s) => s.trim()).filter(Boolean),
      join: $("join").value,
      min_members: Number.parseInt($("min-members").value, 10) || 2,
      verify_sources: $("verify-sources").checked,
      context_paths: state.context.filter((c) => c.path).map((c) => c.path),
    };
    if (state.context.some((c) => c.uploading)) {
      showFormError("Wait for the context files to finish uploading.");
      return;
    }
    const btn = $("start");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const res = await api("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      await openRun(res.run_id, true);
      loadHistory();
    } catch (err) {
      showFormError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Start run";
    }
  }

  // ------------------------------------------------------------------ context files

  function renderContext() {
    $("context-list").replaceChildren(...state.context.map((c) => el("li", { class: c.error ? "bad" : null },
      el("span", { text: c.name + (c.uploading ? " (uploading…)" : c.error ? ": " + c.error : "") }),
      c.uploading ? null : el("button", {
        type: "button", class: "icon-btn", text: "×", "aria-label": "Remove " + c.name,
        onclick: () => { state.context = state.context.filter((x) => x !== c); renderContext(); },
      }))));
  }

  async function onFilesChosen(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    await Promise.all(files.map(async (file) => {
      const entry = { name: file.name, uploading: true, path: null, error: null };
      state.context.push(entry);
      renderContext();
      try {
        const res = await api("/api/context?filename=" + encodeURIComponent(file.name), {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
        });
        entry.path = res.path;
      } catch (err) {
        entry.error = err.message;
      } finally {
        entry.uploading = false;
        renderContext();
      }
    }));
  }

  function addCustomRow() {
    state.rows.push({ id: state.nextRowId++, spec: "", checked: true, custom: true });
    renderModelRows();
    const inputs = $("model-list").querySelectorAll("input[type=text]");
    inputs[inputs.length - 1]?.focus();
  }

  // ------------------------------------------------------------------ run view

  function newRun(id) {
    return {
      id, manifest: null, status: "running", startedAt: null, finishedAt: null, error: null,
      members: new Map(), placeholders: [],
      stages: { research: "pending", synthesis: "pending", validation: "pending", outputs: "pending" },
      ended: false, lastSynthesis: null,
    };
  }

  function mergeManifest(run, m) {
    run.manifest = m;
    if (!(run.status === "cancelled" && m.status === "failed")) run.status = displayStatus(m.status, m.error) || run.status;
    run.startedAt = parseTs(m.created_at) || run.startedAt;
    run.finishedAt = parseTs(m.finished_at) || run.finishedAt;
    if (m.error) run.error = m.error;
    for (const mr of m.members || []) {
      const cur = run.members.get(mr.key) || { key: mr.key };
      // The manifest may lag the event stream; never move a member backwards (e.g. running -> pending).
      const rank = (s) => (!s || s === "pending" ? 0 : s === "running" ? 1 : 2);
      const behind = rank(mr.status) < rank(cur.status);
      Object.assign(cur, {
        spec: mr.spec, label: mr.label, status: behind ? cur.status : mr.status, attempts: mr.attempts || cur.attempts,
        started: parseTs(mr.started_at) || cur.started, finished: parseTs(mr.finished_at) || cur.finished,
        error: mr.error || cur.error,
      });
      run.members.set(mr.key, cur);
    }
    const models = (m.config && m.config.models) || [];
    run.placeholders = run.members.size >= models.length ? [] : placeholdersFor(models, run.members);
  }

  function placeholdersFor(models, members) {
    const left = models.slice();
    for (const m of members.values()) {
      const idx = left.findIndex((spec) => spec === m.spec || (m.key || "").includes(spec.split(":")[0]));
      if (idx >= 0) left.splice(idx, 1);
      else left.shift();
    }
    return left;
  }

  function applyEvent(run, ev) {
    const ts = parseTs(ev.ts) || Date.now();
    const member = ev.member ? run.members.get(ev.member) || { key: ev.member, spec: ev.member, status: "pending" } : null;
    switch (ev.type) {
      case "run.started":
        run.startedAt = run.startedAt || ts;
        run.status = "running";
        break;
      case "member.started":
        run.stages.research = "running";
        Object.assign(member, { status: "running", started: member.started || ts });
        if (ev.detail && !member.label) member.label = ev.detail;
        break;
      case "member.retry":
        member.status = "running";
        member.attempts = (member.attempts || 1) + 1;
        member.note = "Retrying" + (ev.detail ? ": " + ev.detail : "");
        break;
      case "member.finished":
        Object.assign(member, { status: ev.status || "succeeded", finished: ts });
        if (ev.detail && member.status !== "succeeded") member.error = ev.detail;
        announce(member.spec + " " + (STATUS_TEXT[member.status] || member.status).toLowerCase());
        break;
      case "synthesis.started":
        run.stages.research = "done";
        run.stages.synthesis = "running";
        announce("Synthesis started");
        break;
      case "synthesis.finished":
        run.stages.synthesis = ev.status === "failed" ? "failed" : "done";
        break;
      case "validation.finished":
        run.stages.validation = ev.status === "failed" ? "failed" : "done";
        break;
      case "outputs.finished":
        run.stages.outputs = ev.status === "failed" ? "failed" : "done";
        break;
      case "run.finished":
      case "run.failed": {
        const failed = ev.type === "run.failed";
        const cancelled = ev.status === "cancelled" || ev.detail === "cancelled";
        run.status = failed ? (cancelled ? "cancelled" : displayStatus("failed", ev.detail)) : (ev.status || "succeeded");
        if (failed && ev.detail) run.error = ev.detail;
        run.finishedAt = ts;
        run.ended = true;
        for (const s of STAGES) {
          if (run.stages[s.id] === "running") run.stages[s.id] = failed ? "failed" : "done";
          else if (run.stages[s.id] === "pending") run.stages[s.id] = "skipped";
        }
        for (const m of run.members.values()) {
          if (m.status === "running" || m.status === "pending") m.status = failed ? "failed" : "skipped";
        }
        announce("Run " + (STATUS_TEXT[run.status] || run.status).toLowerCase());
        break;
      }
      default:
        break;
    }
    if (member) {
      run.members.set(member.key, member);
      if (run.placeholders.length && !member.placeholderResolved) {
        member.placeholderResolved = true;
        const idx = run.placeholders.findIndex((spec) => member.key.includes(spec.split(":")[0]));
        run.placeholders.splice(idx >= 0 ? idx : 0, 1);
      }
    }
    logEvent(ev);
  }

  function logEvent(ev) {
    const parts = [ev.type];
    if (ev.member) parts.push(ev.member);
    if (ev.status) parts.push(ev.status);
    if (ev.detail) parts.push(ev.detail);
    const when = parseTs(ev.ts);
    $("event-log").append(el("li", null,
      el("span", { class: "t", text: when === null ? "" : new Date(when).toLocaleTimeString() + " " }), parts.join(" · ")));
  }

  function renderRun() {
    const run = state.run;
    if (!run) return;
    const m = run.manifest || {};
    const cfg = m.config || {};
    $("run-empty").hidden = true;
    $("run-body").hidden = false;
    $("run-title").textContent = cfg.objective || run.id;
    $("run-status").replaceWith(Object.assign(pill(run.status), { id: "run-status" }));
    $("run-id").textContent = run.id;
    $("cancel").hidden = run.ended || run.status !== "running";

    const stages = $("stages");
    stages.replaceChildren(...STAGES.map((s) => {
      const st = run.stages[s.id];
      return el("li", { class: "st-" + st, "aria-label": s.label + ": " + STAGE_TEXT[st] },
        s.label, el("span", { class: "state", text: STAGE_TEXT[st] }));
    }));

    const list = $("members");
    const cards = [];
    for (const mem of run.members.values()) {
      const showLabel = mem.label && mem.label !== mem.spec;
      cards.push(el("li", { class: "member s-" + (mem.status || "pending"), "data-key": mem.key },
        el("span", { class: "spec", text: mem.spec || mem.key }),
        showLabel ? el("span", { class: "label", text: mem.label }) : null,
        el("div", { class: "line" }, pill(displayStatus(mem.status, mem.error)), el("span", { class: "elapsed", "data-elapsed": mem.key })),
        mem.note && mem.status === "running" ? el("p", { class: "note", text: mem.note }) : null,
        mem.error && displayStatus(mem.status, mem.error) === mem.status ? el("p", { class: "err", text: mem.error }) : null));
    }
    for (const spec of run.placeholders) {
      cards.push(el("li", { class: "member s-pending" }, el("span", { class: "spec", text: spec }),
        el("div", { class: "line" }, pill("pending"), el("span", { class: "elapsed" }))));
    }
    list.replaceChildren(...cards);

    const errBox = $("run-error");
    errBox.textContent = run.error || "";
    errBox.hidden = !run.error;

    renderArtifacts(run);
    tick();
  }

  function tick() {
    const run = state.run;
    if (!run) return;
    const now = Date.now();
    for (const node of document.querySelectorAll("[data-elapsed]")) {
      const mem = run.members.get(node.getAttribute("data-elapsed"));
      if (!mem || !mem.started) { node.textContent = ""; continue; }
      const end = mem.finished || (mem.status === "running" && !run.ended ? now : null);
      node.textContent = end ? fmtElapsed(end - mem.started) : "";
    }
    const end = run.finishedAt || (run.ended ? null : now);
    $("run-elapsed").textContent = run.startedAt && end ? fmtElapsed(end - run.startedAt) : "";
  }

  function renderArtifacts(run) {
    const arts = (run.manifest && run.manifest.artifacts) || [];
    $("outputs").hidden = arts.length === 0;
    const report = arts.find((a) => /(^|\/)report\.html$/.test(a.path)) || arts.find((a) => /\.html?$/.test(a.path));
    const link = $("open-report");
    link.hidden = !report;
    if (report) link.href = fileUrl(run.id, report.path, false);

    const isMember = (a) => a.path.startsWith("members/") || /member/.test(a.kind || "");
    const ordered = arts.filter((a) => !isMember(a)).concat(arts.filter(isMember));
    $("artifacts").replaceChildren(...ordered.map((a) => el("li", null,
      el("span", { class: "path" }, a.path, el("span", { class: "kind", text: (a.kind || "").replace(/_/g, " ") })),
      el("span", { class: "acts" },
        el("a", { href: fileUrl(run.id, a.path, false), target: "_blank", rel: "noopener noreferrer", text: "Open",
          "aria-label": "Open " + a.path }),
        el("a", { href: fileUrl(run.id, a.path, true), text: "Download", "aria-label": "Download " + a.path })))));

    const synth = arts.find((a) => /(^|\/)synthesis\.md$/.test(a.path));
    if (synth && synth.sha256 !== run.lastSynthesis) {
      run.lastSynthesis = synth.sha256;
      loadSynthesis(run, synth.path);
    } else if (!synth) {
      $("synthesis").hidden = true;
    }
  }

  async function loadSynthesis(run, path) {
    try {
      const resp = await authFetch(fileUrl(run.id, path, false));
      if (!resp.ok || state.run !== run) return;
      const text = await resp.text();
      $("synthesis-body").replaceChildren(renderMarkdown(text));
      $("synthesis").hidden = false;
    } catch (_) {
      $("synthesis").hidden = true;
    }
  }

  async function refreshManifest(run) {
    try {
      const m = await api("/api/runs/" + encodeURIComponent(run.id));
      if (state.run !== run) return;
      mergeManifest(run, m);
      if (m.status && m.status !== "running" && !run.ended && !state.es) run.status = m.status;
      renderRun();
    } catch (_) { /* transient; the event stream drives progress */ }
  }

  async function openRun(runId, focus) {
    if (state.es) { state.es.close(); state.es = null; }
    const run = newRun(runId);
    state.run = run;
    $("event-log").replaceChildren();
    $("synthesis").hidden = true;
    $("synthesis-body").replaceChildren();
    try {
      mergeManifest(run, await api("/api/runs/" + encodeURIComponent(runId)));
    } catch (err) {
      run.error = err.status === 404 ? "This run no longer exists." : err.message;
      run.ended = true;
      run.status = "failed";
    }
    if (state.run !== run) return;
    if (location.hash !== "#run=" + runId) history.replaceState(null, "", "#run=" + encodeURIComponent(runId));
    markCurrentHistory();
    renderRun();
    if (focus) $("run-view").focus();
    if (!run.ended) streamEvents(run);
  }

  function streamEvents(run) {
    const es = new EventSource("/api/runs/" + encodeURIComponent(run.id) + "/events");
    state.es = es;
    let refreshTimer = null;
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      if (state.run !== run) { es.close(); return; }
      applyEvent(run, ev);
      const terminal = ev.type === "run.finished" || ev.type === "run.failed";
      if (terminal) {
        es.close();
        if (state.es === es) state.es = null;
      }
      renderRun();
      // Members/artifacts land in the manifest; refresh it after stage changes (debounced during replay).
      if (terminal || /^(member\.finished|synthesis\.|validation\.|outputs\.)/.test(ev.type)) {
        window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => {
          refreshManifest(run);
          if (terminal) loadHistory();
        }, terminal ? 50 : 400);
      }
    };
    es.onerror = () => {
      if (run.ended) es.close();
      // otherwise EventSource reconnects and the server resumes from Last-Event-ID
    };
  }

  async function onCancel() {
    const run = state.run;
    if (!run) return;
    const btn = $("cancel");
    btn.disabled = true;
    try {
      await api("/api/runs/" + encodeURIComponent(run.id) + "/cancel", { method: "POST" });
      announce("Cancelling run");
    } catch (err) {
      run.error = err.message;
      renderRun();
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ history

  async function loadHistory() {
    const list = $("history");
    let rows;
    try {
      rows = await api("/api/runs");
    } catch (err) {
      list.replaceChildren(el("li", { class: "history-empty", text: "Could not load history: " + err.message }));
      return;
    }
    if (!rows.length) {
      list.replaceChildren(el("li", { class: "history-empty", text: "No runs yet. Your first run will appear here." }));
      return;
    }
    list.replaceChildren(...rows.map((r) => {
      const n = (r.members || []).length;
      const ok = (r.members || []).filter((m) => m.status === "succeeded").length;
      return el("li", null, el("button", {
        type: "button", "data-run": r.run_id, onclick: () => openRun(r.run_id, true),
      },
      el("span", { class: "obj", text: r.objective || r.run_id }),
      el("span", { class: "meta" }, pill(displayStatus(r.status, r.error)), el("span", { text: fmtDate(r.created_at) }),
        n ? el("span", { text: ok + "/" + n + " members succeeded" }) : null)));
    }));
    markCurrentHistory();
  }

  function markCurrentHistory() {
    for (const b of document.querySelectorAll("#history button")) {
      b.setAttribute("aria-current", state.run && b.getAttribute("data-run") === state.run.id ? "true" : "false");
    }
  }

  // ------------------------------------------------------------------ safe markdown (DOM-built, never innerHTML)

  const INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|((?<![A-Za-z0-9])_[^_\s][^_\n]*_(?![A-Za-z0-9]))|(\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

  function inline(text) {
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of text.matchAll(INLINE_RE)) {
      if (m.index > last) frag.append(text.slice(last, m.index));
      const tok = m[0];
      if (m[1]) frag.append(el("code", { text: tok.slice(1, -1) }));
      else if (m[2]) frag.append(el("strong", null, inline(tok.slice(2, -2))));
      else if (m[3] || m[4]) frag.append(el("em", null, inline(tok.slice(1, -1))));
      else if (m[5]) {
        const label = tok.slice(1, tok.indexOf("]("));
        frag.append(el("a", { href: m[6], target: "_blank", rel: "noopener noreferrer nofollow" }, inline(label)));
      } else if (m[7]) {
        frag.append(el("a", { href: tok, target: "_blank", rel: "noopener noreferrer nofollow", text: tok }));
      }
      last = m.index + tok.length;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  }

  function renderMarkdown(src) {
    const root = document.createDocumentFragment();
    const lines = src.replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    const isBlank = (l) => !l || !l.trim();
    const ul = /^\s*[-*+]\s+(.*)$/;
    const ol = /^\s*\d+[.)]\s+(.*)$/;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      const fence = line.match(/^\s*(```|~~~)/);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++;
        root.append(el("pre", null, el("code", { text: body.join("\n") })));
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) {
        const level = Math.min(6, h[1].length + 2);
        root.append(el("h" + level, null, inline(h[2])));
        i++;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { root.append(el("hr")); i++; continue; }
      if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = splitRow(line);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) body.push(splitRow(lines[i++]));
        const table = el("table", null,
          el("thead", null, el("tr", null, ...head.map((c) => el("th", { scope: "col" }, inline(c))))),
          el("tbody", null, ...body.map((r) => el("tr", null, ...r.map((c) => el("td", null, inline(c)))))));
        root.append(el("div", { class: "table-wrap" }, table));
        continue;
      }
      if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
        root.append(el("blockquote", null, renderMarkdown(body.join("\n"))));
        continue;
      }
      if (ul.test(line) || ol.test(line)) {
        const ordered = !ul.test(line);
        const re = ordered ? ol : ul;
        const list = el(ordered ? "ol" : "ul");
        while (i < lines.length && re.test(lines[i])) {
          let item = lines[i++].match(re)[1];
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !ul.test(lines[i]) && !ol.test(lines[i])) {
            item += " " + lines[i++].trim();
          }
          list.append(el("li", null, inline(item)));
        }
        root.append(list);
        continue;
      }
      const para = [];
      while (i < lines.length && !isBlank(lines[i]) && !/^(#{1,6}\s|\s*```|\s*>|\s*\|)/.test(lines[i])
        && !ul.test(lines[i]) && !ol.test(lines[i])) {
        para.push(lines[i++].trim());
      }
      if (!para.length) { para.push(line.trim()); i++; }
      root.append(el("p", null, inline(para.join(" "))));
    }
    return root;
  }

  // ------------------------------------------------------------------ boot

  document.addEventListener("DOMContentLoaded", () => {
    $("run-form").addEventListener("submit", onSubmit);
    $("add-model").addEventListener("click", addCustomRow);
    $("cancel").addEventListener("click", onCancel);
    $("refresh-history").addEventListener("click", loadHistory);
    $("join").addEventListener("change", () => {
      const quorum = $("join").value === "quorum";
      $("min-members").closest(".field").querySelector("label").textContent =
        quorum ? "Quorum size" : "Minimum successful";
    });
    $("context-files").addEventListener("change", onFilesChosen);
    const params = initToken();
    state.ticker = window.setInterval(tick, 1000);
    // Providers first: with a token, that bearer request sets the cookie EventSource needs.
    const openFromHash = (p) => {
      const run = p.get("run");
      if (run && /^[A-Za-z0-9_-]+$/.test(run) && !(state.run && state.run.id === run)) openRun(run, false);
    };
    loadProviders().then(loadHistory).then(() => openFromHash(params));
    window.addEventListener("hashchange", () => {
      const hadToken = new URLSearchParams(location.hash.slice(1)).has("token");
      const p = initToken();
      if (hadToken) loadProviders().then(loadHistory).then(() => openFromHash(p));
      else openFromHash(p);
    });
  });
})();
