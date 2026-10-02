/* Lead Finder — front end.
 *
 * Two jobs: turn the form into a run request, and render the event stream the
 * server pushes back. State lives on the server, so a reload mid-run picks the
 * display straight back up from the snapshot event.
 */

const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then((r) => r.json());

let COUNTRIES = [];
let niches = [];
let cities = [];
let leadRows = [];

/* --------------------------------------------------------------- chips */

function renderChips(host, values, onRemove) {
  host.innerHTML = "";
  values.forEach((value, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = value;
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "Remove";
    x.onclick = () => onRemove(i);
    chip.appendChild(x);
    host.appendChild(chip);
  });
}

function addNiche(value) {
  value = (value || "").trim().replace(/,+$/, "");
  if (!value) return;
  if (!niches.some((n) => n.toLowerCase() === value.toLowerCase())) niches.push(value);
  drawNiches();
}

function drawNiches() {
  renderChips($("nicheChips"), niches, (i) => {
    niches.splice(i, 1);
    drawNiches();
  });
  document.querySelectorAll("#nichePresets button").forEach((b) => {
    b.classList.toggle("on", niches.some((n) => n.toLowerCase() === b.dataset.v));
  });
  drawPlan();
}

function addCity(value) {
  value = (value || "").trim().replace(/,+$/, "");
  if (!value) return;
  if (!cities.some((c) => c.toLowerCase() === value.toLowerCase())) cities.push(value);
  drawCities();
}

function drawCities() {
  renderChips($("cityChips"), cities, (i) => {
    cities.splice(i, 1);
    drawCities();
  });
  document.querySelectorAll("#cityPresets button").forEach((b) => {
    b.classList.toggle("on", cities.some((c) => c.toLowerCase() === b.dataset.v));
  });
  drawPlan();
}

function drawPlan() {
  const lists = niches.length * cities.length;
  const target = parseInt($("target").value, 10) || 0;
  if (!lists) {
    $("plan").innerHTML = "Pick at least one client type and one city.";
    return;
  }
  const each = niches.length === 1 && cities.length === 1;
  $("plan").innerHTML = each
    ? `Up to <b>${target}</b> leads on the <b>${tabName(niches[0])}</b> tab.`
    : `<b>${lists}</b> list${lists > 1 ? "s" : ""} — ${niches.length} client type` +
      `${niches.length > 1 ? "s" : ""} × ${cities.length} cit${cities.length > 1 ? "ies" : "y"}` +
      `, up to <b>${(lists * target).toLocaleString()}</b> leads across ` +
      `<b>${niches.length}</b> tab${niches.length > 1 ? "s" : ""}.`;
}

function tabName(niche) {
  return niche
    .replace(/['\\\[\]*?:/]/g, " ")
    .trim()
    .split(/\s+/)
    .map((w) => (w === w.toUpperCase() ? w : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/* ------------------------------------------------------------ countries */

function drawCountry(code) {
  const select = $("country");
  select.innerHTML = "";
  COUNTRIES.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.code;
    opt.textContent = c.name;
    select.appendChild(opt);
  });
  select.value = code || "PK";
  drawCityPresets();
}

function drawCityPresets() {
  const country = COUNTRIES.find((c) => c.code === $("country").value);
  const list = country ? country.cities : [];
  $("cityList").innerHTML = list.map((c) => `<option value="${c}">`).join("");

  const host = $("cityPresets");
  host.innerHTML = "";
  list.slice(0, 12).forEach((city) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = city;
    b.dataset.v = city.toLowerCase();
    b.onclick = () => {
      const at = cities.findIndex((c) => c.toLowerCase() === city.toLowerCase());
      at >= 0 ? cities.splice(at, 1) : cities.push(city);
      drawCities();
    };
    host.appendChild(b);
  });
  if (list.length > 1) {
    const all = document.createElement("button");
    all.type = "button";
    all.textContent = `+ all ${Math.min(list.length, 12)} cities`;
    all.onclick = () => {
      list.slice(0, 12).forEach(addCity);
    };
    host.appendChild(all);
  }
  drawCities();
}

/* --------------------------------------------------------------- render */

function pill(text, cls) {
  const el = $("statePill");
  el.textContent = text;
  el.className = "pill " + (cls || "");
}

function logLine(entry) {
  const row = document.createElement("div");
  row.className = "line " + (entry.level || "info");
  const time = new Date((entry.at || Date.now() / 1000) * 1000);
  row.innerHTML =
    `<span class="time">${time.toLocaleTimeString([], { hour12: false })}</span>` +
    `<span class="msg"></span>`;
  row.querySelector(".msg").textContent = entry.message;
  const log = $("log");
  const stuck = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.appendChild(row);
  while (log.children.length > 400) log.removeChild(log.firstChild);
  if (stuck) log.scrollTop = log.scrollHeight;
}

function addLeadRow(lead, prepend) {
  const tr = document.createElement("tr");
  const area = (lead.address || lead.city || "").split(",").slice(0, 2).join(",");
  tr.innerHTML =
    `<td></td>` +
    `<td class="tel"></td>` +
    `<td class="dim"></td>` +
    `<td class="dim"></td>` +
    `<td><span class="tag"></span></td>` +
    `<td><a target="_blank" rel="noopener">map</a></td>`;
  const cells = tr.children;
  cells[0].textContent = lead.name || "";
  cells[1].textContent = lead.phone || "—";
  cells[2].textContent = lead.category || "";
  cells[3].textContent = area;
  cells[4].firstChild.textContent = lead.niche || "";
  cells[5].firstChild.href = lead.maps_url || "#";

  const body = $("leadRows");
  prepend && body.firstChild ? body.insertBefore(tr, body.firstChild) : body.appendChild(tr);
  while (body.children.length > 250) body.removeChild(body.lastChild);
  $("leadsEmpty").classList.add("hidden");
  $("leadCount").textContent = leadRows.length;
}

function renderProgress(p, counters) {
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  $("barFill").style.width = pct + "%";
  const phase =
    { scrape: "Searching", phones: "Recovering phones", emails: "Looking for emails" }[
      p.phase
    ] || "Working";
  $("barLabel").textContent = p.total
    ? `${phase} ${p.done}/${p.total} · ${p.label || ""}`
    : p.label || "Ready when you are.";
  if (counters) {
    $("cFound").textContent = counters.found || 0;
    $("cPhones").textContent = counters.phones || 0;
    $("cEmails").textContent = counters.emails || 0;
    $("cPushed").textContent = counters.pushed || 0;
  }
}

function renderSummary(rows) {
  const body = $("summaryRows");
  body.innerHTML = "";
  const real = (rows || []).filter((r) => r.leads);
  real.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><span class="tag"></span></td><td class="dim"></td><td></td>` +
      `<td class="dim"></td><td class="dim"></td><td></td>` +
      `<td><a href="/api/export?niche=${encodeURIComponent(r.niche || "")}` +
      `&city=${encodeURIComponent(r.city || "")}">CSV</a></td>`;
    const c = tr.children;
    c[0].firstChild.textContent = r.niche ? tabName(r.niche) : "(earlier runs)";
    c[1].textContent = r.city || "";
    c[2].textContent = r.leads;
    c[3].textContent = r.phones || 0;
    c[4].textContent = r.emails || 0;
    c[5].textContent = r.pushed || 0;
    body.appendChild(tr);
  });
  $("summaryEmpty").classList.toggle("hidden", real.length > 0);
}

function setRunning(on) {
  $("startBtn").disabled = on;
  $("startBtn").textContent = on ? "Running…" : "Find leads";
  $("stopBtn").classList.toggle("hidden", !on);
}

/* ---------------------------------------------------------------- events */

const handlers = {
  snapshot(state) {
    $("log").innerHTML = "";
    (state.log || []).forEach(logLine);
    $("leadRows").innerHTML = "";
    leadRows = state.leads || [];
    leadRows.forEach((l) => addLeadRow(l, false));
    renderProgress(state.progress || {}, state.counters);
    if (state.current) handlers.task_started(state.current);
    if (state.sheet && state.sheet.url) handlers.sheet(state.sheet);
    if (state.blocked) handlers.blocked(state.blocked);
    setRunning(state.running);
    if (state.running) pill("Running", "running");
    else if (state.finished_at) pill(state.cancelled ? "Stopped" : "Done",
                                     state.cancelled ? "stopped" : "done");
    else pill("Idle", "");
  },
  log: logLine,
  lead(lead) {
    leadRows.push(lead);
    addLeadRow(lead, true);
  },
  run_started(d) {
    setRunning(true);
    pill("Running", "running");
    logLine({ level: "step", message: `Starting ${d.tasks.length} list(s).` });
  },
  task_started(d) {
    $("currentTask").textContent = `${tabName(d.niche)} — ${d.city}`;
    $("taskCount").textContent = d.total ? `list ${d.index} of ${d.total}` : "";
    logLine({
      level: "step",
      message: `→ ${d.niche} in ${d.city} (tab: ${d.tab})`,
    });
  },
  progress(d) {
    renderProgress(d, null);
    if (d.found !== undefined) $("cFound").textContent = d.found;
  },
  stats(d) {
    $("cFound").textContent = d.found || 0;
  },
  task_done(d) {
    logLine({
      level: "good",
      message:
        `✓ ${d.niche} · ${d.city}: ${d.found} leads, ${d.with_phone} with a phone` +
        (d.pushed ? `, ${d.pushed} new rows in the sheet` : ""),
    });
    refreshSummary();
  },
  sheet(d) {
    const link = $("sheetLink");
    link.href = d.url || "#";
    $("sheetLabel").textContent = d.title || "Google Sheet";
    $("sheetDot").className = "dot ok";
  },
  blocked(d) {
    const card = $("blockedCard");
    card.textContent = d.message;
    card.classList.remove("hidden");
  },
  unblocked() {
    $("blockedCard").classList.add("hidden");
  },
  run_done(d) {
    setRunning(false);
    pill(d.cancelled ? "Stopped" : "Done", d.cancelled ? "stopped" : "done");
    $("barFill").style.width = "100%";
    refreshSummary();
  },
};

function listen() {
  const source = new EventSource("/api/events");
  Object.keys(handlers).forEach((name) => {
    source.addEventListener(name, (e) => {
      try {
        handlers[name](JSON.parse(e.data));
      } catch (err) {
        console.error(name, err);
      }
    });
  });
  source.onerror = () => {
    // Flask closes idle streams; reconnect quietly.
    source.close();
    setTimeout(listen, 2500);
  };
}

/* ---------------------------------------------------------------- wiring */

function collect() {
  return {
    niches,
    cities,
    country: $("country").value,
    target: parseInt($("target").value, 10) || 100,
    sheet_url: $("sheetUrl").value,
    push_to_sheets: $("pushToSheets").checked,
    phone_lookup: $("phoneLookup").checked,
    find_email: $("findEmail").checked,
    show_browser: $("showBrowser").checked,
    write_csv: $("writeCsv").checked,
    fresh: $("fresh").checked,
    tile_km: parseFloat($("tileKm").value) || 3,
    max_tiles: parseInt($("maxTiles").value, 10) || 400,
    email_limit: parseInt($("emailLimit").value, 10) || 0,
    phone_cap: parseInt($("phoneCap").value, 10) || 0,
    min_delay: parseFloat($("minDelay").value) || 1.8,
    max_delay: parseFloat($("maxDelay").value) || 4,
  };
}

function applySettings(s) {
  $("sheetUrl").value = s.sheet_url || "";
  $("target").value = s.target || 100;
  $("pushToSheets").checked = s.push_to_sheets !== false;
  $("phoneLookup").checked = s.phone_lookup !== false;
  $("findEmail").checked = !!s.find_email;
  $("showBrowser").checked = s.headless === false;
  $("writeCsv").checked = s.write_csv !== false;
  $("fresh").checked = !!s.fresh;
  $("tileKm").value = s.tile_km ?? 3;
  $("maxTiles").value = s.max_tiles ?? 400;
  $("emailLimit").value = s.email_limit ?? 25;
  $("phoneCap").value = s.phone_cap ?? 250;
  $("minDelay").value = s.min_delay ?? 1.8;
  $("maxDelay").value = s.max_delay ?? 4;
  niches = (s.niches || []).slice();
  cities = (s.cities || []).slice();
}

async function refreshSummary() {
  const data = await api("/api/summary");
  renderSummary(data.rows);
}

async function start() {
  $("formError").textContent = "";
  const res = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collect()),
  });
  const data = await res.json();
  if (!data.ok) {
    $("formError").textContent = data.error || "Could not start.";
    return;
  }
  setRunning(true);
}

function chipInput(input, add) {
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add(input.value);
      input.value = "";
    } else if (e.key === "Backspace" && !input.value) {
      add === addNiche ? niches.pop() : cities.pop();
      add === addNiche ? drawNiches() : drawCities();
    }
  });
  // Picking from the datalist fires input, not keydown.
  input.addEventListener("change", () => {
    if (input.value) {
      add(input.value);
      input.value = "";
    }
  });
  input.addEventListener("blur", () => {
    if (input.value) {
      add(input.value);
      input.value = "";
    }
  });
}

async function boot() {
  const data = await api("/api/bootstrap");
  COUNTRIES = data.countries;

  $("nicheList").innerHTML = data.presets.map((p) => `<option value="${p}">`).join("");
  const presetHost = $("nichePresets");
  data.presets.forEach((preset) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = preset;
    b.dataset.v = preset.toLowerCase();
    b.onclick = () => {
      const at = niches.findIndex((n) => n.toLowerCase() === preset.toLowerCase());
      at >= 0 ? niches.splice(at, 1) : niches.push(preset);
      drawNiches();
    };
    presetHost.appendChild(b);
  });

  applySettings(data.settings);
  drawCountry(data.settings.country);
  drawNiches();

  if (!data.credentials.ready) {
    $("setupBanner").classList.remove("hidden");
    $("setupText").textContent =
      ` Leads will still be collected and saved as CSV — connect a service account to fill your sheet.`;
    $("sheetDot").className = "dot bad";
  } else {
    $("helpEmail").textContent = data.credentials.email;
  }
  if (data.settings.sheet_url) {
    $("sheetLink").href = data.settings.sheet_url;
  }

  renderSummary(data.summary);
  handlers.snapshot(data.state);
  listen();
}

/* events */
chipInput($("nicheInput"), addNiche);
chipInput($("cityInput"), addCity);
$("country").addEventListener("change", drawCityPresets);
$("target").addEventListener("input", drawPlan);
$("targetQuick").addEventListener("click", (e) => {
  if (e.target.dataset.n) {
    $("target").value = e.target.dataset.n;
    drawPlan();
  }
});
$("startBtn").onclick = start;
$("stopBtn").onclick = () => fetch("/api/stop", { method: "POST" });
$("setupHelpBtn").onclick = () => $("helpDialog").showModal();
$("checkSheetBtn").onclick = async () => {
  const out = $("checkResult");
  out.textContent = "Checking…";
  out.style.color = "";
  const data = await api("/api/sheet-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sheet_url: $("sheetUrl").value }),
  });
  if (data.ok) {
    out.style.color = "var(--good)";
    out.textContent =
      `Connected to "${data.title}" — ${data.tabs.length} tab(s), ` +
      `${data.existing} lead(s) already there.`;
    $("sheetLink").href = data.url;
    $("sheetLabel").textContent = data.title;
    $("sheetDot").className = "dot ok";
    $("setupBanner").classList.add("hidden");
  } else {
    out.style.color = "var(--bad)";
    out.textContent = data.error;
    if (data.credentials && data.credentials.email) {
      $("helpEmail").textContent = data.credentials.email;
    }
  }
};
document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".tab-body").forEach((b) => b.classList.add("hidden"));
    $("tab-" + tab.dataset.tab).classList.remove("hidden");
  };
});

boot();
