/* LMS Cache web UI. Plain JS, no build step. */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hb = (n) => {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(i >= 3 ? 1 : 0)) + " " + u[i];
  };
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, (S.data?.now || Date.now() / 1000) - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  const dateShort = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "2-digit", month: "short", day: "numeric" }) : "");
  const fmtInt = (n) => (n == null ? "" : Number(n).toLocaleString());

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || JSON.stringify(j); } catch (_) {}
      throw new Error(msg);
    }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }

  let toastTimer;
  function toast(msg, bad = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (bad ? " bad" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3200);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (_) {}
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.left = "-9999px";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) {}
    document.body.removeChild(ta);
    return ok;
  }

  // ---------- state ----------
  const S = {
    data: null,
    view: "library",
    machine: localStorage.getItem("lmsc.machine") || "",
    filter: "",
    fmt: "any",
    search: { q: "", fmt: "any", sort: "downloads", results: null, loading: false, error: null },
    editing: null,
  };

  const STATES = [["absent", "—", "Not available on this machine"], ["cached", "Cached", "Copied to the local disk"], ["linked", "Linked", "Symlinked to the library share"]];

  async function refresh() {
    try {
      S.data = await api("/api/state");
    } catch (e) {
      toast("Cannot reach LMS Cache: " + e.message, true);
      return;
    }
    ensureMachine();
    render();
  }

  function ensureMachine() {
    const names = (S.data?.machines || []).map((m) => m.name);
    if (!names.includes(S.machine)) S.machine = names[0] || "";
    const sel = $("#machine-select");
    sel.innerHTML = names.length
      ? names.map((n) => `<option value="${esc(n)}" ${n === S.machine ? "selected" : ""}>${esc(n)}</option>`).join("")
      : `<option value="">no machines yet</option>`;
  }

  function currentMachine() {
    return (S.data?.machines || []).find((m) => m.name === S.machine) || null;
  }

  // ---------- rendering ----------
  function render() {
    $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === S.view));
    $$(".view").forEach((v) => { v.hidden = v.id !== "view-" + S.view; });
    const active = (S.data?.jobs || []).filter((j) => ["queued", "running", "finalizing"].includes(j.status)).length;
    const badge = $("#dl-badge");
    badge.hidden = active === 0; badge.textContent = active;
    if (!S.data) return;
    ({ library: renderLibrary, search: renderSearch, downloads: renderDownloads, machines: renderMachines, settings: renderSettings })[S.view]();
  }

  // ----- library -----
  function renderLibrary() {
    const d = S.data;
    const me = currentMachine();
    const run = $("#runbar");
    if (!d.machines.length) {
      run.innerHTML = `<span class="label">No machines yet.</span><span class="muted">Add one on the Machines page to get a one-liner that applies states on that machine.</span>`;
    } else if (me) {
      const rep = me.report;
      run.innerHTML = `
        <span class="label">Run on ${esc(me.name)}</span>
        <code id="runbar-cmd">${esc(me.one_liner)}</code>
        <button class="primary" data-copy="${esc(me.one_liner)}">Copy</button>
        <span class="note">Fetches a fresh interactive script from this NAS and applies the wanted states below. Nothing is installed on the machine.
          ${rep ? `Last report ${esc(ago(rep.at))}, ${esc(hb(rep.free_bytes))} free locally.` : `<b>No report yet</b>: run it once so this table can show what is on ${esc(me.name)}.`}
        </span>`;
    }

    const models = d.models.filter((m) => {
      if (S.fmt !== "any" && m.format !== S.fmt) return false;
      if (S.filter && !m.id.toLowerCase().includes(S.filter)) return false;
      return true;
    });
    const totalBytes = d.models.reduce((a, m) => a + (m.total_bytes || 0), 0);
    $("#lib-summary").textContent = `${d.models.length} models, ${hb(totalBytes)} in the library · ${hb(d.disk.free)} free on the NAS`;

    const machineCols = d.machines.map((m) => `<th class="${m.name === S.machine ? "me" : ""}" title="${m.report ? "reported " + esc(ago(m.report.at)) : "no report yet"}">${esc(m.name)}${m.report ? "" : " <span class='muted'>?</span>"}</th>`).join("");
    let html = `<thead><tr><th>Model</th><th>Format</th><th>Quant</th><th>Size</th><th>Added</th>${machineCols}<th></th></tr></thead><tbody>`;
    if (!models.length) {
      html += `<tr><td colspan="${6 + d.machines.length}"><div class="empty">${d.models.length ? "Nothing matches the filter." : "The library is empty. Use Search to download a model."}</div></td></tr>`;
    }
    for (const m of models) {
      html += `<tr>
        <td class="model" title="${esc(m.id)}"><a href="#" data-model-detail="${esc(m.id)}">${esc(m.id)}</a></td>
        <td><span class="tag ${esc(m.format)}">${esc(m.format)}</span></td>
        <td class="muted">${esc((m.quants || []).join(", "))}</td>
        <td class="num">${hb(m.total_bytes)}</td>
        <td class="muted">${dateShort(m.added_at)}</td>`;
      for (const mc of d.machines) {
        const cell = d.cells?.[mc.name]?.[m.id] || { reported: null, bytes: 0, intent: null, pending: false };
        html += `<td class="${mc.name === S.machine ? "me" : ""}">${cellHtml(mc.name, m, cell)}</td>`;
      }
      html += `<td class="actions"><button class="danger" data-delete="${esc(m.id)}" title="Delete from the library">Delete</button></td></tr>`;
    }
    html += "</tbody>";
    $("#lib-table").innerHTML = html;
  }

  function cellHtml(machine, model, cell) {
    const rep = cell.reported;
    const intent = cell.intent;
    const actual = rep === "partial" ? "cached" : rep;
    const shown = actual || intent || "absent";
    const unknown = rep === null;
    let html = `<div class="seg ${unknown ? "unknown" : ""}" data-machine="${esc(machine)}" data-model="${esc(model.id)}">`;
    for (const [st, label, title] of STATES) {
      const cls = [];
      if (st === shown) cls.push("on", st);
      if (intent === st && cell.pending) cls.push("want");
      html += `<button class="${cls.join(" ")}" data-state="${st}" title="${title}${intent === st && cell.pending ? " (wanted, not applied yet)" : ""}">${label}</button>`;
    }
    html += `</div>`;
    if (rep === "partial") html += `<div class="sub warn">partial: ${hb(cell.bytes)} of ${hb(model.total_bytes)}</div>`;
    else if (cell.pending) html += `<div class="sub pending">wanted: ${esc(intent)}</div>`;
    else if (unknown && intent) html += `<div class="sub">wanted: ${esc(intent)}, no report yet</div>`;
    return html;
  }

  async function onSegClick(btn) {
    const seg = btn.closest(".seg");
    const machine = seg.dataset.machine, model = seg.dataset.model, state = btn.dataset.state;
    const cell = S.data.cells?.[machine]?.[model] || {};
    const actual = cell.reported === "partial" ? "cached" : cell.reported;
    try {
      if (cell.intent === state) {
        await api(`/api/machines/${encodeURIComponent(machine)}/models/${model}`, { method: "DELETE" });
        toast(`Cleared the wanted state for ${model} on ${machine}.`);
      } else if (actual === state && !cell.intent) {
        toast(`${model} is already ${state} on ${machine}.`);
        return;
      } else {
        await api(`/api/machines/${encodeURIComponent(machine)}/models/${model}`, { method: "PUT", body: JSON.stringify({ state }) });
        const label = { absent: "not available", cached: "cached locally", linked: "linked" }[state];
        toast(`Wanted: ${label} on ${machine}. Run the ${machine} one-liner to apply it.`);
      }
      await refresh();
    } catch (e) { toast(e.message, true); }
  }

  async function openModelDetail(id) {
    openDrawer(esc(id), `<div class="muted">Loading…</div>`);
    try {
      const m = await api(`/api/library/${id}`);
      const hf = m.hf || {};
      let html = `<div class="kv">
        <div>Format</div><div><span class="tag ${esc(m.format)}">${esc(m.format)}</span> ${esc((m.quants || []).join(", "))}</div>
        <div>Size</div><div>${hb(m.total_bytes)} in ${m.file_count} files</div>
        <div>Added</div><div>${dateShort(m.added_at)}</div>
        ${m.revision ? `<div>Revision</div><div><code>${esc(m.revision.slice(0, 12))}</code></div>` : ""}
        ${hf.params ? `<div>Parameters</div><div>${fmtInt(hf.params)}</div>` : ""}
        ${hf.downloads != null ? `<div>Hub downloads</div><div>${fmtInt(hf.downloads)}</div>` : ""}
        ${m.source && m.source.kind === "upload" ? `<div>Source</div><div>uploaded from ${esc(m.source.machine || "a machine")} ${dateShort(m.source.at)}</div>` : ""}
        <div>On the share</div><div><code>lmstudio/${esc(m.id)}</code></div>
        <div>Hub page</div><div><a href="https://huggingface.co/${esc(m.id)}" target="_blank" rel="noopener">huggingface.co/${esc(m.id)}</a></div>
      </div>
      <h4>Files</h4><ul class="files">${m.files.map((f) => `<li><span>${esc(f.path)}</span><span>${hb(f.size)}</span></li>`).join("")}</ul>`;
      $("#drawer-body").innerHTML = html;
    } catch (e) { $("#drawer-body").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  }

  async function deleteModel(id) {
    const linked = S.data.machines.filter((m) => (S.data.cells?.[m.name]?.[id] || {}).reported === "linked").map((m) => m.name);
    let msg = `Delete ${id} from the library on the NAS?`;
    if (linked.length) msg += `\n\nThese machines link to it and will lose it: ${linked.join(", ")}.`;
    msg += "\n\nLocal cached copies on machines are not touched.";
    if (!confirm(msg)) return;
    try {
      await api(`/api/library/${id}`, { method: "DELETE" });
      toast(`Deleted ${id}.`);
      await refresh();
    } catch (e) { toast(e.message, true); }
  }

  // ----- search -----
  function renderSearch() {
    const box = $("#search-results");
    const s = S.search;
    if (s.loading) { box.innerHTML = `<div class="empty">Searching Hugging Face…</div>`; return; }
    if (s.error) { box.innerHTML = `<div class="empty error">${esc(s.error)}</div>`; return; }
    if (!s.results) { box.innerHTML = `<div class="empty">Search the Hub, or paste an <code>owner/repo</code> id to open it directly. Downloads land in the library in LM Studio's folder layout.</div>`; return; }
    const direct = /^[\w.-]+\/[\w.-]+$/.test(s.q.trim()) ? s.q.trim() : null;
    let html = "";
    if (direct && !s.results.some((r) => r.id.toLowerCase() === direct.toLowerCase())) {
      html += `<div class="card" style="border-radius:8px 8px 0 0;border-bottom:none"><span class="muted">Open repository directly:</span> <a href="#" data-open-repo="${esc(direct)}">${esc(direct)}</a></div>`;
    }
    if (!s.results.length) { box.innerHTML = html + `<div class="empty">No results.</div>`; return; }
    html += `<table class="grid"><thead><tr><th>Model</th><th>Format</th><th>Downloads</th><th>Likes</th><th>Updated</th><th></th><th></th></tr></thead><tbody>`;
    for (const r of s.results) {
      html += `<tr>
        <td class="model"><a href="#" data-open-repo="${esc(r.id)}">${esc(r.id)}</a> <a class="muted" href="https://huggingface.co/${esc(r.id)}" target="_blank" rel="noopener" title="Open on huggingface.co">↗</a></td>
        <td><span class="tag ${esc(r.format)}">${esc(r.format)}</span></td>
        <td class="num">${fmtInt(r.downloads)}</td>
        <td class="num">${fmtInt(r.likes)}</td>
        <td class="muted">${r.updated ? esc(r.updated.slice(0, 10)) : ""}</td>
        <td>${r.gated ? `<span class="tag gated">gated</span>` : ""} ${r.in_library ? `<span class="tag have">in library</span>` : ""}</td>
        <td class="actions"><button data-open-repo="${esc(r.id)}">Files…</button></td></tr>`;
    }
    html += "</tbody></table>";
    box.innerHTML = html;
  }

  async function doSearch() {
    const s = S.search;
    s.q = $("#search-q").value;
    s.sort = $("#search-sort").value;
    s.loading = true; s.error = null; render();
    try {
      const res = await api(`/api/search?q=${encodeURIComponent(s.q)}&format=${encodeURIComponent(s.fmt)}&sort=${encodeURIComponent(s.sort)}&limit=40`);
      s.results = res.results;
    } catch (e) { s.error = e.message; }
    s.loading = false; render();
  }

  async function openRepo(id) {
    openDrawer(esc(id), `<div class="muted">Loading file list from Hugging Face…</div>`);
    let info;
    try { info = await api(`/api/repo/${id}`); }
    catch (e) { $("#drawer-body").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
    const pref = (info.preferred_quant || "").toUpperCase();
    const have = new Set(info.library_files || []);
    let html = `<div class="kv">
      <div>Format</div><div><span class="tag ${esc(info.format)}">${esc(info.format)}</span> ${info.gated ? '<span class="tag gated">gated: accept the terms on the Hub page first</span>' : ""} ${info.in_library ? '<span class="tag have">in library</span>' : ""}</div>
      ${info.params ? `<div>Parameters</div><div>${fmtInt(info.params)}</div>` : ""}
      <div>Downloads</div><div>${fmtInt(info.downloads)} · ${fmtInt(info.likes)} likes · updated ${info.updated ? esc(info.updated.slice(0, 10)) : "?"}</div>
      <div>Total size</div><div>${hb(info.total_bytes)} in ${info.files.length} files</div>
      <div>Hub page</div><div><a href="https://huggingface.co/${esc(info.id)}" target="_blank" rel="noopener">huggingface.co/${esc(info.id)}</a></div>
    </div>
    <h4>Choose what to download</h4>
    <form id="repo-form">`;
    for (const g of info.groups) {
      const allHave = g.files.length && g.files.every((f) => have.has(f.path));
      const checked = g.kind === "all" || (g.kind === "quant" && pref && g.key.toUpperCase() === pref && !allHave);
      html += `<div class="group">
        <label><input type="checkbox" name="group" value="${esc(g.key)}" ${checked ? "checked" : ""}>
          <span>${esc(g.label)}${allHave ? ' <span class="tag have">in library</span>' : ""}</span>
          <span class="size">${g.files.length} file${g.files.length === 1 ? "" : "s"} · ${hb(g.size)}</span></label>
        <details><summary>files</summary><ul>${g.files.map((f) => `<li><span>${esc(f.path)}</span><span>${hb(f.size)}</span></li>`).join("")}</ul></details>
      </div>`;
    }
    html += `</form>
    <div class="drawer-foot"><span id="repo-selected" class="muted"></span><span class="spacer"></span><button id="repo-download" class="primary">Download to library</button></div>`;
    $("#drawer-body").innerHTML = html;
    const groupsByKey = Object.fromEntries(info.groups.map((g) => [g.key, g]));
    const update = () => {
      const keys = $$("#repo-form input:checked").map((i) => i.value);
      const files = keys.flatMap((k) => groupsByKey[k].files);
      $("#repo-selected").textContent = files.length ? `${files.length} file${files.length === 1 ? "" : "s"}, ${hb(files.reduce((a, f) => a + f.size, 0))}` : "nothing selected";
      $("#repo-download").disabled = !files.length;
    };
    $("#repo-form").addEventListener("change", update);
    update();
    $("#repo-download").onclick = async () => {
      const keys = $$("#repo-form input:checked").map((i) => i.value);
      const whole = keys.includes("all") && info.groups.length === 1;
      const files = keys.flatMap((k) => groupsByKey[k].files.map((f) => f.path));
      $("#repo-download").disabled = true;
      try {
        await api("/api/downloads", { method: "POST", body: JSON.stringify({ repo_id: info.id, files, whole_repo: whole }) });
        toast(`Queued ${info.id}.`);
        closeDrawer();
        S.view = "downloads";
        await refresh();
      } catch (e) { toast(e.message, true); $("#repo-download").disabled = false; }
    };
  }

  // ----- downloads -----
  function renderDownloads() {
    const jobs = S.data.jobs;
    const active = jobs.filter((j) => ["queued", "running", "finalizing"].includes(j.status));
    $("#dl-summary").textContent = jobs.length ? `${active.length} active, ${jobs.length - active.length} finished` : "";
    const list = $("#dl-list");
    if (!jobs.length) { list.innerHTML = `<div class="empty">No downloads yet.</div>`; return; }
    list.innerHTML = jobs.map((j) => {
      const pct = j.total_bytes ? Math.min(100, (100 * j.bytes_done) / j.total_bytes) : 0;
      const eta = j.status === "running" && j.rate > 1 ? Math.round((j.total_bytes - j.bytes_done) / j.rate) : null;
      const etaTxt = eta == null ? "" : eta > 3600 ? `${Math.round(eta / 3600)} h left` : eta > 60 ? `${Math.round(eta / 60)} min left` : `${eta}s left`;
      const what = j.whole_repo ? "whole repository" : `${j.files.length} file${j.files.length === 1 ? "" : "s"}`;
      const buttons = [];
      if (["queued", "running", "finalizing"].includes(j.status)) buttons.push(`<button data-job-cancel="${j.id}">Cancel</button>`);
      if (["failed", "cancelled"].includes(j.status)) buttons.push(`<button data-job-retry="${j.id}">Retry</button>`);
      if (!["running", "finalizing"].includes(j.status)) buttons.push(`<button data-job-remove="${j.id}" class="danger">Remove</button>`);
      return `<div class="card">
        <div class="head"><span class="title">${esc(j.repo_id)}</span><span class="status ${esc(j.status)}">${esc(j.status)}</span><span class="meta">${esc(what)} · ${hb(j.total_bytes)}</span><span class="spacer"></span>${buttons.join(" ")}</div>
        <div class="progress ${j.status === "done" ? "done" : j.status === "failed" ? "failed" : ""}"><div style="width:${j.status === "done" ? 100 : pct}%"></div></div>
        <div class="meta">${j.status === "running" ? `${hb(j.bytes_done)} of ${hb(j.total_bytes)} · ${hb(j.rate)}/s ${etaTxt}` : j.status === "done" ? `finished ${ago(j.finished_at)}` : j.status === "queued" ? "waiting for a slot" : j.status === "finalizing" ? "checking files and moving into the library" : ""}</div>
        ${j.error ? `<div class="error">${esc(j.error)}</div>` : ""}
        ${j.log && j.status !== "done" ? `<details><summary class="muted">log</summary><pre class="log">${esc(j.log)}</pre></details>` : ""}
      </div>`;
    }).join("");
  }

  // ----- machines -----
  function renderMachines() {
    const d = S.data;
    const list = $("#machine-list");
    if (!d.machines.length) {
      list.innerHTML = `<div class="card"><h3>No machines yet</h3><p class="muted">Add each Mac or Linux box that runs LM Studio. Each one gets a single command that fetches an interactive script from this NAS: it mounts the library share, shows what is local versus wanted, and applies changes. Nothing is installed.</p></div>`;
    } else {
      list.innerHTML = d.machines.map((m) => {
        const rep = m.report;
        const foreign = d.foreign?.[m.name] || [];
        return `<div class="card" data-machine-card="${esc(m.name)}">
          <div class="head"><span class="title">${esc(m.name)}</span><span class="tag">${m.os === "mac" ? "macOS" : "Linux"}</span>
            <span class="meta">${rep ? `reported ${esc(ago(rep.at))} · ${esc(hb(rep.free_bytes))} free of ${esc(hb(rep.total_bytes))} · ${rep.count} local model${rep.count === 1 ? "" : "s"}` : "never reported: run the command once"}</span>
            <span class="spacer"></span><button data-machine-edit="${esc(m.name)}">Edit</button><button class="danger" data-machine-delete="${esc(m.name)}">Delete</button></div>
          <div class="kv">
            <div>Models folder</div><div><code>${esc(m.models_dir)}</code></div>
            <div>Library mounted at</div><div><code>${esc(m.mount)}</code> <span class="muted">(share //${esc(d.settings.smb_host || "this host")}/${esc(d.settings.smb_share)} as ${esc(m.smb_user || d.settings.smb_user || "guest")})</span></div>
            <div>Link mode</div><div>${m.link_mode === "files" ? "per-file symlinks" : "folder symlink"}</div>
          </div>
          <div class="row"><span class="muted">Run in a terminal on ${esc(m.name)}:</span><button data-copy="${esc(m.one_liner)}">Copy</button></div>
          <code class="cmd">${esc(m.one_liner)}</code>
          <div class="muted" style="margin-top:6px;font-size:12.5px">First time: choose <b>m</b> in the menu to mount the share at login. Append <code>lmsc --apply</code> to skip the menu, or <code>lmsc --report</code> to only refresh this page.</div>
          ${foreign.length ? `<div class="row"><span class="muted">Local models not in the library:</span></div><ul class="files">${foreign.map((f) => `<li><span>${esc(f.id)}</span><span>${hb(f.bytes)} · <a href="#" data-add-foreign="${esc(f.id)}">get from the Hub</a> · <a href="#" data-upload-cmd="${esc(m.name)}" data-upload-model="${esc(f.id)}">upload from ${esc(m.name)}</a></span></li>`).join("")}</ul>` : ""}
        </div>`;
      }).join("");
    }
    const form = $("#machine-form");
    const det = $("#add-machine");
    if (S.editing) {
      const m = d.machines.find((x) => x.name === S.editing);
      if (m) {
        det.open = true;
        det.querySelector("summary").textContent = `Edit ${m.name}`;
        form.editing.value = m.name; form.name.value = m.name;
        form.os.value = m.os; form.models_dir.value = m.models_dir; form.mount.value = m.mount;
        form.smb_user.value = m.smb_user || ""; form.link_mode.value = m.link_mode || "dir";
      }
    } else {
      det.querySelector("summary").textContent = "Add a machine";
      form.editing.value = "";
    }
  }

  async function saveMachine(ev) {
    ev.preventDefault();
    const f = ev.target;
    const body = {
      name: f.name.value.trim(), rename_from: f.editing.value || null, os: f.os.value, models_dir: f.models_dir.value.trim() || null, mount: f.mount.value.trim() || null,
      smb_user: f.smb_user.value.trim() || null, link_mode: f.link_mode.value,
    };
    try {
      await api("/api/machines", { method: "POST", body: JSON.stringify(body) });
      toast(`Saved ${body.name}.`);
      if (body.rename_from && S.machine === body.rename_from) { S.machine = body.name; localStorage.setItem("lmsc.machine", S.machine); }
      S.editing = null; f.reset(); $("#add-machine").open = false;
      if (!S.machine) S.machine = body.name;
      await refresh();
    } catch (e) { toast(e.message, true); }
  }

  // ----- settings -----
  let settingsFilled = false;
  function renderSettings() {
    const d = S.data;
    const f = $("#settings-form");
    if (!settingsFilled) {
      const s = d.settings;
      f.preferred_quant.value = s.preferred_quant || "";
      f.max_parallel.value = s.max_parallel || 1;
      f.xet_high_performance.checked = !!s.xet_high_performance;
      f.verify_checksums.checked = !!s.verify_checksums;
      f.public_url.value = s.public_url || "";
      f.smb_host.value = s.smb_host || "";
      f.smb_share.value = s.smb_share || "models";
      f.smb_user.value = s.smb_user || "guest";
      settingsFilled = true;
    }
    $("#token-status").textContent = d.settings.has_token ? `Stored: ${d.settings.hf_token_hint}. Leave the field empty to keep it.` : "No token stored.";
    $("#smb-status").textContent = d.settings.has_smb_password ? "A password is stored. Leave the field empty to keep it." : "No password stored.";
    $('[data-secret-clear="hf_token"]').hidden = !d.settings.has_token;
    $('[data-secret-clear="smb_password"]').hidden = !d.settings.has_smb_password;
    $("#library-info").innerHTML = `Library folder in the container: <code>${esc(d.library_dir)}</code><br>NAS volume: ${hb(d.disk.used)} used, ${hb(d.disk.free)} free of ${hb(d.disk.total)}<br>Last scan ${esc(ago(d.scanned_at))} · ${d.models.length} models`;
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const f = ev.target;
    const body = {
      preferred_quant: f.preferred_quant.value.trim(), max_parallel: Number(f.max_parallel.value) || 1,
      xet_high_performance: f.xet_high_performance.checked, verify_checksums: f.verify_checksums.checked,
      public_url: f.public_url.value.trim(), smb_host: f.smb_host.value.trim(), smb_share: f.smb_share.value.trim() || "models",
      smb_user: f.smb_user.value.trim() || "guest",
    };
    if (f.smb_password.value && f.smb_password.dataset.revealed !== "1") body.smb_password = f.smb_password.value;
    const tok = f.hf_token.value.trim();
    if (tok && f.hf_token.dataset.revealed !== "1") body.hf_token = tok;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      for (const k of ["hf_token", "smb_password"]) { f[k].value = ""; f[k].type = "password"; delete f[k].dataset.revealed; $(`[data-secret-toggle="${k}"]`).textContent = "Show"; }
      $("#settings-status").textContent = "Saved.";
      setTimeout(() => { $("#settings-status").textContent = ""; }, 2500);
      settingsFilled = false;
      await refresh();
    } catch (e) { toast(e.message, true); }
  }

  // ----- drawer -----
  function openDrawer(title, body) {
    $("#drawer-title").innerHTML = title;
    $("#drawer-body").innerHTML = body;
    $("#drawer").hidden = false;
  }
  function closeDrawer() { $("#drawer").hidden = true; }

  // ---------- events ----------
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("button, a");
    if (!t) return;
    if (t.dataset.view) { S.view = t.dataset.view; render(); return; }
    if (t.dataset.copy != null) { ev.preventDefault(); toast((await copyText(t.dataset.copy)) ? "Copied." : "Copy failed: select the command and copy it manually.", false); return; }
    if (t.closest(".seg") && t.dataset.state) { onSegClick(t); return; }
    if (t.dataset.modelDetail) { ev.preventDefault(); openModelDetail(t.dataset.modelDetail); return; }
    if (t.dataset.delete) { deleteModel(t.dataset.delete); return; }
    if (t.dataset.openRepo) { ev.preventDefault(); openRepo(t.dataset.openRepo); return; }
    if (t.dataset.uploadCmd) {
      ev.preventDefault();
      const m = S.data.machines.find((x) => x.name === t.dataset.uploadCmd);
      const cmd = `${m.one_liner} lmsc --upload ${t.dataset.uploadModel}`;
      openDrawer(`Upload ${esc(t.dataset.uploadModel)} from ${esc(m.name)}`, `<p>Run this in a terminal on <b>${esc(m.name)}</b>. It streams the model's files to the NAS with resume support and adds the folder to the library; the copy on ${esc(m.name)} then counts as <i>cached</i>.</p><code class="cmd">${esc(cmd)}</code><div class="row" style="margin-top:10px"><button class="primary" data-copy="${esc(cmd)}">Copy</button></div><p class="muted" style="margin-top:14px">Or run the plain one-liner: the interactive menu lists these models as <b>u1</b>, <b>u2</b>, and so on. Type that label to upload one, or press <b>u</b> to pick from the list.</p>`);
      return;
    }
    if (t.dataset.addForeign) { ev.preventDefault(); S.view = "search"; $("#search-q").value = t.dataset.addForeign; render(); openRepo(t.dataset.addForeign); return; }
    if (t.dataset.jobCancel) { try { await api(`/api/downloads/${t.dataset.jobCancel}/cancel`, { method: "POST" }); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.dataset.jobRetry) { try { await api(`/api/downloads/${t.dataset.jobRetry}/retry`, { method: "POST" }); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.dataset.jobRemove) { try { await api(`/api/downloads/${t.dataset.jobRemove}`, { method: "DELETE" }); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.dataset.machineEdit) { S.editing = t.dataset.machineEdit; render(); $("#add-machine").scrollIntoView({ behavior: "smooth" }); return; }
    if (t.dataset.machineDelete) {
      if (!confirm(`Remove machine ${t.dataset.machineDelete} from LMS Cache? Nothing on the machine itself changes.`)) return;
      try { await api(`/api/machines/${encodeURIComponent(t.dataset.machineDelete)}`, { method: "DELETE" }); await refresh(); } catch (e) { toast(e.message, true); }
      return;
    }
    if (t.id === "drawer-close") { closeDrawer(); return; }
    if (t.id === "dl-clear") { try { await api("/api/downloads/clear", { method: "POST" }); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.id === "rescan") { try { const r = await api("/api/library/rescan", { method: "POST" }); toast(`Rescanned: ${r.count} models.`); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.id === "machine-cancel") { S.editing = null; $("#machine-form").reset(); $("#add-machine").open = false; render(); return; }
    if (t.dataset.secretToggle) {
      const input = $("#settings-form")[t.dataset.secretToggle];
      if (input.type === "text") {                       // hide: back to a masked field; a revealed stored value is dropped, typed text is kept
        if (input.dataset.revealed === "1") { input.value = ""; delete input.dataset.revealed; }
        input.type = "password"; t.textContent = "Show"; return;
      }
      if (!input.value) {                                // nothing typed: fetch the stored value from the NAS
        try {
          const r = await api(`/api/settings/reveal/${t.dataset.secretToggle}`);
          if (!r.value) { toast("Nothing is stored yet."); return; }
          input.value = r.value; input.dataset.revealed = "1";
        } catch (e) { toast(e.message, true); return; }
      }
      input.type = "text"; t.textContent = "Hide"; return;
    }
    if (t.dataset.secretClear) {
      const key = t.dataset.secretClear;
      if (!confirm(key === "hf_token" ? "Remove the stored Hugging Face token?" : "Remove the stored SMB password? Machines will no longer be able to mount the share unattended.")) return;
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: "" }) });
        const input = $("#settings-form")[key]; input.value = ""; input.type = "password"; delete input.dataset.revealed;
        $(`[data-secret-toggle="${key}"]`).textContent = "Show";
        toast("Removed."); await refresh();
      } catch (e) { toast(e.message, true); }
      return;
    }
    if (t.closest("#lib-fmt") && t.dataset.fmt) { S.fmt = t.dataset.fmt; $$("#lib-fmt button").forEach((b) => b.classList.toggle("on", b === t)); render(); return; }
    if (t.closest("#search-fmt") && t.dataset.fmt) { ev.preventDefault(); S.search.fmt = t.dataset.fmt; $$("#search-fmt button").forEach((b) => b.classList.toggle("on", b === t)); if (S.search.results) doSearch(); return; }
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
  $("#lib-filter").addEventListener("input", (ev) => { S.filter = ev.target.value.trim().toLowerCase(); render(); });
  $("#search-form").addEventListener("submit", (ev) => { ev.preventDefault(); doSearch(); });
  $("#search-sort").addEventListener("change", () => { if (S.search.results) doSearch(); });
  $("#machine-select").addEventListener("change", (ev) => { S.machine = ev.target.value; localStorage.setItem("lmsc.machine", S.machine); render(); });
  $("#machine-form").addEventListener("submit", saveMachine);
  $("#machine-form").os.addEventListener("change", (ev) => {
    const f = ev.target.form;
    f.mount.placeholder = ev.target.value === "mac" ? "/Users/Shared/lmscache" : "/mnt/lmscache";
  });
  $("#settings-form").addEventListener("submit", saveSettings);

  // live updates: SSE nudges plus a slow poll as a fallback
  let debounce;
  const nudge = () => { clearTimeout(debounce); debounce = setTimeout(refresh, 250); };
  try {
    const es = new EventSource("/api/events");
    es.addEventListener("state", nudge);
    es.addEventListener("progress", nudge);
  } catch (_) {}
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 20000);

  refresh();
})();
