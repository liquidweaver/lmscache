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
  const S = { data: null, view: "library", machine: localStorage.getItem("lmsc.machine") || "", filter: "", fmt: "any", editing: null };
  const STATES = [["absent", "—", "Not available on this machine"], ["cached", "Cached", "Copied to the local disk"], ["linked", "Linked", "Symlinked to the library share"]];

  async function refresh() {
    try { S.data = await api("/api/state"); }
    catch (e) { toast("Cannot reach LMS Cache: " + e.message, true); return; }
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

  function currentMachine() { return (S.data?.machines || []).find((m) => m.name === S.machine) || null; }

  // ---------- rendering ----------
  function render() {
    $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === S.view));
    $$(".view").forEach((v) => { v.hidden = v.id !== "view-" + S.view; });
    if (!S.data) return;
    ({ library: renderLibrary, machines: renderMachines, settings: renderSettings })[S.view]();
  }

  // ----- library -----
  function renderLibrary() {
    const d = S.data;
    const me = currentMachine();
    const run = $("#runbar");
    if (!d.machines.length) {
      run.innerHTML = `<span class="label">No machines yet.</span><span class="muted">Add one on the Machines page to get a one-liner that uploads models from it and applies states on it.</span>`;
    } else if (me) {
      const rep = me.report;
      run.innerHTML = `
        <span class="label">Run on ${esc(me.name)}</span>
        <code id="runbar-cmd">${esc(me.one_liner)}</code>
        <button class="primary" data-copy="${esc(me.one_liner)}">Copy</button>
        <span class="note">Fetches a fresh interactive script from this NAS: it applies the wanted states below, uploads local quants the library lacks, and reports back. Nothing is installed on the machine.
          ${rep ? (rep.stale ? `<b>Report from an older client</b>: run it again.` : `Last report ${esc(ago(rep.at))}, ${esc(hb(rep.free_bytes))} free locally.`) : `<b>No report yet</b>: run it once so this table can show what is on ${esc(me.name)}.`}
        </span>`;
    }

    const rows = [];
    for (const m of d.models) for (const v of m.variants || []) rows.push({ m, v });
    const shown = rows.filter(({ m, v }) => {
      if (S.fmt !== "any" && m.format !== S.fmt) return false;
      if (S.filter && !(m.id + " " + v.key).toLowerCase().includes(S.filter)) return false;
      return true;
    });
    const totalBytes = d.models.reduce((a, m) => a + (m.total_bytes || 0), 0);
    $("#lib-summary").textContent = `${rows.length} quant${rows.length === 1 ? "" : "s"} in ${d.models.length} repo${d.models.length === 1 ? "" : "s"}, ${hb(totalBytes)} in the library · ${hb(d.disk.free)} free on the NAS`;

    const machineCols = d.machines.map((m) => `<th class="${m.name === S.machine ? "me" : ""}" title="${m.report ? "reported " + esc(ago(m.report.at)) : "no report yet"}">${esc(m.name)}${m.report && !m.report.stale ? "" : " <span class='muted'>?</span>"}</th>`).join("");
    let html = `<thead><tr><th>Model</th><th>Quant</th><th>Format</th><th>Size</th><th>Added</th>${machineCols}<th></th></tr></thead><tbody>`;
    if (!shown.length) {
      html += `<tr><td colspan="${6 + d.machines.length}"><div class="empty">${rows.length ? "Nothing matches the filter." : "The library is empty. Run a machine's one-liner and upload a model you want to keep with u&lt;number&gt;."}</div></td></tr>`;
    }
    let lastRepo = null;
    for (const { m, v } of shown) {
      const first = m.id !== lastRepo; lastRepo = m.id;
      html += `<tr class="${first ? "repo-first" : "repo-more"}">
        <td class="model" title="${esc(m.id)}">${first ? `<a href="#" data-model-detail="${esc(m.id)}">${esc(m.id)}</a>` : `<span class="muted">〃</span>`}</td>
        <td><span class="tag quant">${esc(v.label)}</span></td>
        <td><span class="tag ${esc(m.format)}">${esc(m.format)}</span></td>
        <td class="num">${hb(v.bytes)}</td>
        <td class="muted">${dateShort(m.added_at)}</td>`;
      for (const mc of d.machines) {
        const cell = d.cells?.[mc.name]?.[v.id] || { reported: null, bytes: 0, intent: null, pending: false };
        html += `<td class="${mc.name === S.machine ? "me" : ""}">${cellHtml(mc.name, v.id, v.bytes, cell)}</td>`;
      }
      html += `<td class="actions"><button class="danger" data-delete-variant="${esc(v.id)}" title="Delete this quant from the library">Delete</button></td></tr>`;
    }
    html += "</tbody>";
    $("#lib-table").innerHTML = html;
  }

  function cellHtml(machine, variantId, totalBytes, cell) {
    const rep = cell.reported;
    const intent = cell.intent;
    const actual = rep === "partial" ? "cached" : rep;
    const shown = actual || intent || "absent";
    const unknown = rep === null;
    let html = `<div class="seg ${unknown ? "unknown" : ""}" data-machine="${esc(machine)}" data-model="${esc(variantId)}">`;
    for (const [st, label, title] of STATES) {
      const cls = [];
      if (st === shown) cls.push("on", st);
      if (intent === st && cell.pending) cls.push("want");
      html += `<button class="${cls.join(" ")}" data-state="${st}" title="${title}${intent === st && cell.pending ? " (wanted, not applied yet)" : ""}">${label}</button>`;
    }
    html += `</div>`;
    if (rep === "partial") html += `<div class="sub warn">partial: ${hb(cell.bytes)} of ${hb(totalBytes)}</div>`;
    else if (cell.pending) html += `<div class="sub pending">wanted: ${esc(intent)}</div>`;
    else if (unknown && intent) html += `<div class="sub">wanted: ${esc(intent)}, no report yet</div>`;
    return html;
  }

  async function onSegClick(btn) {
    const seg = btn.closest(".seg");
    const machine = seg.dataset.machine, vid = seg.dataset.model, state = btn.dataset.state;
    const cell = S.data.cells?.[machine]?.[vid] || {};
    const actual = cell.reported === "partial" ? "cached" : cell.reported;
    try {
      if (cell.intent === state) {
        await api(`/api/machines/${encodeURIComponent(machine)}/models/${vid}`, { method: "DELETE" });
        toast(`Cleared the wanted state for ${vid} on ${machine}.`);
      } else if (actual === state && !cell.intent) {
        toast(`${vid} is already ${state} on ${machine}.`);
        return;
      } else {
        await api(`/api/machines/${encodeURIComponent(machine)}/models/${vid}`, { method: "PUT", body: JSON.stringify({ state }) });
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
      const html = `<div class="kv">
        <div>Format</div><div><span class="tag ${esc(m.format)}">${esc(m.format)}</span> ${(m.variants || []).length} quant${(m.variants || []).length === 1 ? "" : "s"}</div>
        <div>Size</div><div>${hb(m.total_bytes)} in ${m.file_count} files</div>
        <div>Added</div><div>${dateShort(m.added_at)}</div>
        ${m.source && m.source.kind === "upload" ? `<div>Source</div><div>uploaded from ${esc(m.source.machine || "a machine")} ${dateShort(m.source.at)}</div>` : ""}
        <div>On the share</div><div><code>lmstudio/${esc(m.id)}</code></div>
        <div>Hub page</div><div><a href="https://huggingface.co/${esc(m.id)}" target="_blank" rel="noopener">huggingface.co/${esc(m.id)}</a></div>
      </div>
      ${(m.variants || []).map((v) => `<h4><span class="tag quant">${esc(v.label)}</span> ${hb(v.bytes)}</h4><ul class="files">${v.files.map((f) => `<li><span>${esc(f.path)}</span><span>${hb(f.size)}</span></li>`).join("")}</ul>`).join("")}
      ${(m.shared || []).length ? `<h4>Shared by every quant</h4><ul class="files">${m.shared.map((f) => `<li><span>${esc(f.path)}</span><span>${hb(f.size)}</span></li>`).join("")}</ul>` : ""}`;
      $("#drawer-body").innerHTML = html;
    } catch (e) { $("#drawer-body").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  }

  async function deleteVariant(vid) {
    const [repo, key] = vid.split("@");
    const linked = S.data.machines.filter((m) => (S.data.cells?.[m.name]?.[vid] || {}).reported === "linked").map((m) => m.name);
    let msg = `Delete ${vid} from the library on the NAS?`;
    if (linked.length) msg += `\n\nThese machines link to it and will lose it: ${linked.join(", ")}.`;
    msg += "\n\nLocal cached copies on machines are not touched.";
    if (!confirm(msg)) return;
    try {
      await api(`/api/library/${repo}?variant=${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`Deleted ${vid}.`);
      await refresh();
    } catch (e) { toast(e.message, true); }
  }

  // ----- machines -----
  function renderMachines() {
    const d = S.data;
    const list = $("#machine-list");
    if (!d.machines.length) {
      list.innerHTML = `<div class="card"><h3>No machines yet</h3><p class="muted">Add each Mac or Linux box that runs LM Studio. Each one gets a single command that fetches an interactive script from this NAS: it mounts the library share, shows what is local versus wanted per quant, uploads local quants you want to keep, and applies changes. Nothing is installed.</p></div>`;
    } else {
      list.innerHTML = d.machines.map((m) => {
        const rep = m.report;
        const foreign = d.foreign?.[m.name] || [];
        return `<div class="card" data-machine-card="${esc(m.name)}">
          <div class="head"><span class="title">${esc(m.name)}</span><span class="tag">${m.os === "mac" ? "macOS" : "Linux"}</span>
            <span class="meta">${rep ? (rep.stale ? `report from an older client version (${esc(ago(rep.at))}): run the command again` : `reported ${esc(ago(rep.at))} · ${esc(hb(rep.free_bytes))} free of ${esc(hb(rep.total_bytes))} · ${rep.count} local model folder${rep.count === 1 ? "" : "s"}`) : "never reported: run the command once"}</span>
            <span class="spacer"></span><button data-machine-edit="${esc(m.name)}">Edit</button><button class="danger" data-machine-delete="${esc(m.name)}">Delete</button></div>
          <div class="kv">
            <div>Models folder</div><div><code>${esc(m.models_dir)}</code></div>
            <div>Library mounted at</div><div><code>${esc(m.mount)}</code> <span class="muted">(share //${esc(d.settings.smb_host || "this host")}/${esc(d.settings.smb_share)} as ${esc(m.smb_user || d.settings.smb_user || "guest")})</span></div>
          </div>
          <div class="row"><span class="muted">Run in a terminal on ${esc(m.name)}:</span><button data-copy="${esc(m.one_liner)}">Copy</button></div>
          <code class="cmd">${esc(m.one_liner)}</code>
          <div class="muted" style="margin-top:6px;font-size:12.5px">First time: choose <b>m</b> in the menu to mount the share at login. Append <code>lmsc --apply</code> to skip the menu, <code>lmsc --report</code> to only refresh this page, or <code>lmsc --upload publisher/repo@QUANT</code>.</div>
          ${foreign.length ? `<div class="row"><span class="muted">Local quants not in the library:</span></div><ul class="files">${foreign.map((f) => `<li><span>${esc(f.id)}</span><span>${hb(f.bytes)} · <a href="#" data-upload-cmd="${esc(m.name)}" data-upload-model="${esc(f.id)}">upload from ${esc(m.name)}</a></span></li>`).join("")}</ul>` : ""}
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
        form.smb_user.value = m.smb_user || "";
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
      smb_user: f.smb_user.value.trim() || null,
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
      f.public_url.value = s.public_url || "";
      f.smb_host.value = s.smb_host || "";
      f.smb_share.value = s.smb_share || "models";
      f.smb_user.value = s.smb_user || "guest";
      settingsFilled = true;
    }
    $("#smb-status").textContent = d.settings.has_smb_password ? "A password is stored. Leave the field empty to keep it." : "No password stored.";
    $('[data-secret-clear="smb_password"]').hidden = !d.settings.has_smb_password;
    $("#library-info").innerHTML = `Library folder in the container: <code>${esc(d.library_dir)}</code><br>NAS volume: ${hb(d.disk.used)} used, ${hb(d.disk.free)} free of ${hb(d.disk.total)}<br>Last scan ${esc(ago(d.scanned_at))} · ${d.models.length} repos`;
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const f = ev.target;
    const body = {
      public_url: f.public_url.value.trim(), smb_host: f.smb_host.value.trim(), smb_share: f.smb_share.value.trim() || "models",
      smb_user: f.smb_user.value.trim() || "guest",
    };
    if (f.smb_password.value && f.smb_password.dataset.revealed !== "1") body.smb_password = f.smb_password.value;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      f.smb_password.value = ""; f.smb_password.type = "password"; delete f.smb_password.dataset.revealed; $('[data-secret-toggle="smb_password"]').textContent = "Show";
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
    if (t.dataset.deleteVariant) { deleteVariant(t.dataset.deleteVariant); return; }
    if (t.dataset.uploadCmd) {
      ev.preventDefault();
      const m = S.data.machines.find((x) => x.name === t.dataset.uploadCmd);
      const cmd = `${m.one_liner} lmsc --upload ${t.dataset.uploadModel}`;
      openDrawer(`Upload ${esc(t.dataset.uploadModel)} from ${esc(m.name)}`, `<p>Run this in a terminal on <b>${esc(m.name)}</b>. It streams this quant's files to the NAS with resume support and adds them to the library; the copy on ${esc(m.name)} then counts as <i>cached</i>.</p><code class="cmd">${esc(cmd)}</code><div class="row" style="margin-top:10px"><button class="primary" data-copy="${esc(cmd)}">Copy</button></div><p class="muted" style="margin-top:14px">Or run the plain one-liner: the interactive menu lists these quants as <b>u1</b>, <b>u2</b>, and so on. Type that label to upload one, or press <b>u</b> to pick from the list.</p>`);
      return;
    }
    if (t.dataset.machineEdit) { S.editing = t.dataset.machineEdit; render(); $("#add-machine").scrollIntoView({ behavior: "smooth" }); return; }
    if (t.dataset.machineDelete) {
      if (!confirm(`Remove machine ${t.dataset.machineDelete} from LMS Cache? Nothing on the machine itself changes.`)) return;
      try { await api(`/api/machines/${encodeURIComponent(t.dataset.machineDelete)}`, { method: "DELETE" }); await refresh(); } catch (e) { toast(e.message, true); }
      return;
    }
    if (t.dataset.secretToggle) {
      const input = $("#settings-form")[t.dataset.secretToggle];
      if (input.type === "text") {
        if (input.dataset.revealed === "1") { input.value = ""; delete input.dataset.revealed; }
        input.type = "password"; t.textContent = "Show"; return;
      }
      if (!input.value) {
        try {
          const r = await api(`/api/settings/reveal/${t.dataset.secretToggle}`);
          if (!r.value) { toast("Nothing is stored yet."); return; }
          input.value = r.value; input.dataset.revealed = "1";
        } catch (e) { toast(e.message, true); return; }
      }
      input.type = "text"; t.textContent = "Hide"; return;
    }
    if (t.dataset.secretClear) {
      if (!confirm("Remove the stored SMB password? Machines using the global account will no longer mount the share unattended.")) return;
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify({ smb_password: "" }) });
        const input = $("#settings-form").smb_password; input.value = ""; input.type = "password"; delete input.dataset.revealed;
        $('[data-secret-toggle="smb_password"]').textContent = "Show";
        toast("Removed."); await refresh();
      } catch (e) { toast(e.message, true); }
      return;
    }
    if (t.id === "drawer-close") { closeDrawer(); return; }
    if (t.id === "rescan") { try { const r = await api("/api/library/rescan", { method: "POST" }); toast(`Rescanned: ${r.count} repos.`); await refresh(); } catch (e) { toast(e.message, true); } return; }
    if (t.id === "machine-cancel") { S.editing = null; $("#machine-form").reset(); $("#add-machine").open = false; render(); return; }
    if (t.closest("#lib-fmt") && t.dataset.fmt) { S.fmt = t.dataset.fmt; $$("#lib-fmt button").forEach((b) => b.classList.toggle("on", b === t)); render(); return; }
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
  $("#lib-filter").addEventListener("input", (ev) => { S.filter = ev.target.value.trim().toLowerCase(); render(); });
  $("#machine-select").addEventListener("change", (ev) => { S.machine = ev.target.value; localStorage.setItem("lmsc.machine", S.machine); render(); });
  $("#machine-form").addEventListener("submit", saveMachine);
  $("#machine-form").os.addEventListener("change", (ev) => { ev.target.form.mount.placeholder = ev.target.value === "mac" ? "/Users/Shared/lmscache" : "/mnt/lmscache"; });
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
