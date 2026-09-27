// Live job status in the header; reload the page when a job finishes.
(function () {
  const el = document.getElementById("jobstatus");
  let wasRunning = el && !el.classList.contains("idle");
  async function poll() {
    try {
      const r = await fetch("/api/status");
      const s = await r.json();
      if (s.running) {
        el.classList.remove("idle");
        el.innerHTML = '<span class="spinner"></span> ' + esc(s.running) + " <small>" + esc(s.detail || "") + "</small>";
        wasRunning = true;
      } else {
        if (wasRunning && !document.querySelector("input:focus, textarea:focus, details[open].pick")) {
          location.reload();
          return;
        }
        wasRunning = false;
        el.classList.add("idle");
        el.innerHTML = s.last ? "<small>" + esc(s.last) + "</small>" : "";
      }
    } catch (e) { /* ignore */ }
    setTimeout(poll, wasRunning ? 2000 : 6000);
  }
  function esc(t) { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; }
  if (el) setTimeout(poll, 1500);

  // Lazy-load fragments (e.g. other editions) when their <details> is opened.
  document.querySelectorAll("[data-load]").forEach((box) => {
    const details = box.closest("details");
    const load = async () => {
      if (box.dataset.loaded) return;
      box.dataset.loaded = "1";
      const r = await fetch(box.dataset.load);
      box.innerHTML = await r.text();
    };
    if (!details || details.open) load();
    else details.addEventListener("toggle", () => details.open && load());
  });
})();
