/* Ollama status — shared by every page (served from /static/ollama.js).

   The footer dot was the only sign the model was off, and it sat below the
   fold on a page whose top half keeps working either way (captures fall back
   to rules). So an outage now shows as a banner at the top of the page, with
   the broken step named and the button that fixes it. When everything works
   it shrinks to a small pill that can still run a full test on demand.     */
(function () {
  const POLL_MS = 30000;
  let timer = null, busy = false;

  const css = `
  #sb-ollama{font:13px/1.45 system-ui,sans-serif;margin:0 0 14px;border-radius:10px;
    border:1px solid var(--line,#8884);background:var(--card,#fff1);color:inherit}
  #sb-ollama.bad{border-color:#d33;background:#d331}
  #sb-ollama.warn{border-color:#d90;background:#d901}
  #sb-ollama.pill{position:fixed;right:12px;bottom:12px;z-index:50;margin:0;
    padding:4px 10px;cursor:pointer;border-radius:999px;background:var(--card,#fff);box-shadow:0 1px 4px #0003}
  #sb-ollama .in{padding:10px 14px}
  #sb-ollama h4{margin:0 0 6px;font-size:14px}
  #sb-ollama ul{margin:6px 0;padding-left:0;list-style:none}
  #sb-ollama li{margin:2px 0}
  #sb-ollama .fix{opacity:.85;margin-left:22px;display:block}
  #sb-ollama button{margin:6px 6px 0 0;padding:4px 10px;border-radius:6px;border:1px solid currentColor;
    background:transparent;color:inherit;cursor:pointer;font:inherit}
  #sb-ollama button.go{background:#d33;border-color:#d33;color:#fff}
  #sb-ollama pre{white-space:pre-wrap;font-size:11px;max-height:160px;overflow:auto;margin:4px 0 0}
  #sb-ollama .x{float:right}`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  const box = document.createElement("div");
  box.id = "sb-ollama";
  const mount = () => (document.querySelector("main") || document.body)
    .insertBefore(box, (document.querySelector("main") || document.body).firstChild);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();

  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const ICON = {ok: "✅", warn: "⚠️", fail: "❌", skip: "➖"};

  async function call(path, opts) {
    const r = await fetch(path, opts);
    const data = await r.json();
    if (data && data.error) throw new Error(data.error);
    return data;
  }

  function pill(c) {
    box.className = "pill";
    box.title = "Click to run a full Ollama test";
    box.innerHTML = `🟢 Ollama ready`;
    box.onclick = () => run(true, true);
    void c;
  }

  function render(c, note) {
    box.onclick = null;
    box.title = "";
    const cls = c.state === "offline" ? "bad" : c.state === "degraded" ? "warn" : "";
    box.className = cls;
    const actions = new Set(c.steps.map(s => s.action).filter(Boolean));
    const steps = c.steps.map(s =>
      `<li>${ICON[s.status] || ""} <b>${esc(s.label)}</b> — ${esc(s.detail)}` +
      (s.fix ? `<span class="fix">→ ${esc(s.fix)}</span>` : "") + `</li>`).join("");
    box.innerHTML = `<div class="in">
      <button class="x" data-a="hide" title="Hide until the next check">✕</button>
      <h4>${c.state === "ready" ? "🟢" : c.state === "offline" ? "🔴" : "🟠"} ${esc(c.headline)}</h4>
      ${note ? `<div>${esc(note)}</div>` : ""}
      <ul>${steps}</ul>
      ${actions.has("start") ? `<button class="go" data-a="start">Fix: start Ollama</button>` : ""}
      ${actions.has("pull") ? `<button class="go" data-a="pull">Pull missing models</button>` : ""}
      <button data-a="test">Test again (full)</button>
      <small>checked ${esc(c.checked_at)} · ${esc(c.url)}</small>
      ${c.log && c.log.length ? `<details><summary>server.log (last lines)</summary><pre>${esc(c.log.join("\n"))}</pre></details>` : ""}
    </div>`;
    box.querySelectorAll("button").forEach(b => b.onclick = ev => {
      ev.stopPropagation();
      const a = b.dataset.a;
      if (a === "hide") { box.className = "pill"; box.innerHTML = c.state === "ready" ? "🟢 Ollama ready" : "🔴 Ollama off"; box.onclick = () => render(c); }
      else if (a === "test") run(true, true);
      else fix(a === "pull");
    });
  }

  function show(c, forceOpen, note) {
    if (c.state === "ready" && !forceOpen) pill(c);
    else if (c.state === "off") { box.className = ""; box.innerHTML = ""; }
    else render(c, note);
    clearTimeout(timer);
    if (c.state === "offline" || c.state === "degraded") timer = setTimeout(() => run(false, false), POLL_MS);
  }

  async function run(deep, forceOpen) {
    if (busy) return;
    busy = true;
    if (forceOpen) { box.className = ""; box.innerHTML = `<div class="in">Testing Ollama${deep ? " (loading the model can take ~30 s)" : ""}…</div>`; }
    try { show(await call(`/api/llm/check?deep=${deep ? 1 : 0}`), forceOpen); }
    catch (e) { box.className = "bad"; box.innerHTML = `<div class="in">Could not check Ollama: ${esc(e.message)}</div>`; }
    finally { busy = false; }
  }

  async function fix(pull) {
    if (busy) return;
    busy = true;
    box.className = "warn";
    box.innerHTML = `<div class="in">${pull ? "Pulling models — this can take several minutes; leave the page open…" : "Starting Ollama…"}</div>`;
    try {
      const r = await call("/api/llm/fix", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-SB-Action": "1"},
        body: JSON.stringify({pull}),
      });
      let note = "";
      if (r.start && r.start.started) note = "Started Ollama.";
      else if (r.start && r.start.reason && r.start.reason !== "already running") note = "Could not start: " + r.start.reason;
      if (r.pull) note += (r.pull.pulled.length ? ` Pulled: ${r.pull.pulled.join(", ")}.` : "") +
        (Object.keys(r.pull.failed).length ? ` Failed: ${Object.entries(r.pull.failed).map(([m, e]) => m + " (" + e + ")").join("; ")}` : "");
      busy = false;
      show(r.check, true, note.trim());
    } catch (e) {
      busy = false;
      box.className = "bad";
      box.innerHTML = `<div class="in">Fix failed: ${esc(e.message)}</div>`;
    }
  }

  window.sbOllamaCheck = () => run(true, true);
  run(false, false);
})();
