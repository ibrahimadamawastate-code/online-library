(function () {
  const menuToggle = document.querySelector("[data-menu-toggle]");
  const menu = document.querySelector("[data-menu]");
  if (menuToggle && menu) {
    menuToggle.addEventListener("click", () => {
      const open = menu.classList.toggle("open");
      menuToggle.setAttribute("aria-expanded", String(open));
      document.body.classList.toggle("menu-open", open);
    });
  }

  const overlay = document.querySelector("[data-search-overlay]");
  const openSearch = document.querySelector("[data-search-open]");
  const closeSearch = document.querySelector("[data-search-close]");
  function setSearch(open) {
    if (!overlay) return;
    overlay.hidden = !open;
    if (open) {
      const input = overlay.querySelector("input");
      if (input) input.focus();
    }
  }
  if (openSearch) openSearch.addEventListener("click", () => setSearch(true));
  if (closeSearch) closeSearch.addEventListener("click", () => setSearch(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      event.preventDefault();
      setSearch(true);
    }
    if (event.key === "Escape") setSearch(false);
  });

  document.querySelectorAll("[data-suggest]").forEach((input) => {
    const list = input.parentElement.querySelector("[data-suggest-list]");
    if (!list) return;
    let timer;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 2) {
          list.hidden = true;
          list.innerHTML = "";
          return;
        }
        const response = await fetch("/api/suggest?q=" + encodeURIComponent(q));
        const data = await response.json();
        list.innerHTML = (data.results || []).map((item) =>
          `<a href="/book/${item.id}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.author)} · ${escapeHtml(item.category)}</span></a>`
        ).join("");
        list.hidden = !data.results.length;
      }, 160);
    });
  });

  document.querySelectorAll("[data-bookmark]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("[data-bookmark-btn]");
      const response = await fetch(form.action, {
        method: "POST",
        headers: {
          "X-CSRF-Token": document.querySelector('meta[name="csrf"]').content,
          "X-Requested-With": "fetch",
        },
      });
      if (!response.ok) {
        form.submit();
        return;
      }
      const data = await response.json();
      if (button) button.textContent = data.saved ? "Saved" : "Bookmark";
    });
  });

  document.querySelectorAll(".toast").forEach((toast) => {
    setTimeout(() => toast.remove(), 4200);
  });

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }
})();
