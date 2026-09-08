(() => {
  const table = document.querySelector("[data-sortable]");
  if (!table) return;
  const body = table.tBodies[0];
  table.querySelectorAll("th[data-sort-column]").forEach((header) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sort-button";
    button.dataset.column = header.dataset.sortColumn;
    button.dataset.direction = "desc";
    button.textContent = header.textContent.trim();
    header.replaceChildren(button);
    button.addEventListener("click", () => {
      const column = Number(button.dataset.column);
      const direction = button.dataset.direction === "asc" ? "desc" : "asc";
      button.dataset.direction = direction;
      table.querySelectorAll("th[aria-sort]").forEach((item) => item.removeAttribute("aria-sort"));
      header.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");
      const rows = Array.from(body.rows);
      rows.sort((a, b) => {
        const av = a.cells[column].dataset.sort ?? a.cells[column].textContent.trim();
        const bv = b.cells[column].dataset.sort ?? b.cells[column].textContent.trim();
        if (header.dataset.sortType === "number") {
          const an = av.trim() === "" ? NaN : Number(av);
          const bn = bv.trim() === "" ? NaN : Number(bv);
          if (!Number.isFinite(an)) return Number.isFinite(bn) ? 1 : 0;
          if (!Number.isFinite(bn)) return -1;
          return (an - bn) * (direction === "asc" ? 1 : -1);
        }
        return av.localeCompare(bv, undefined, { numeric: true }) * (direction === "asc" ? 1 : -1);
      });
      rows.forEach((row) => body.appendChild(row));
    });
  });
})();
