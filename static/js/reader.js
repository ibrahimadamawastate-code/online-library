(function () {
  const reader = document.querySelector(".reader");
  if (!reader) return;
  const article = document.querySelector(".reader-article");
  const bar = document.querySelector("[data-progress-bar]");
  const storedTheme = localStorage.getItem("ol-theme") || "cream";
  const storedSize = Number(localStorage.getItem("ol-font") || 18);
  applySize(storedSize);
  reader.dataset.theme = storedTheme;

  document.querySelectorAll("[data-theme]").forEach((button) => {
    button.addEventListener("click", () => {
      reader.dataset.theme = button.dataset.theme;
      localStorage.setItem("ol-theme", button.dataset.theme);
    });
  });
  document.querySelectorAll("[data-font]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = Math.min(26, Math.max(16, (Number(localStorage.getItem("ol-font") || 18) + Number(button.dataset.font))));
      applySize(next);
      localStorage.setItem("ol-font", String(next));
    });
  });

  let last = -1;
  function report() {
    if (!article) return;
    const rect = article.getBoundingClientRect();
    const total = article.offsetHeight - window.innerHeight;
    const percent = total <= 0 ? 100 : Math.round(Math.min(1, Math.max(0, -rect.top / total)) * 100);
    if (bar) bar.style.width = percent + "%";
    if (reader.dataset.loggedIn !== "1" || Math.abs(percent - last) < 8) return;
    last = percent;
    fetch("/read/" + reader.dataset.book + "/progress", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf"]').content,
      },
      body: JSON.stringify({ chapter: Number(reader.dataset.chapter), percent }),
    });
  }
  document.addEventListener("scroll", report, { passive: true });
  report();

  function applySize(size) {
    document.documentElement.style.setProperty("--read-size", size + "px");
  }
})();
