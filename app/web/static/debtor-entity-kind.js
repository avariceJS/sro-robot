(() => {
  const form = document.querySelector("[data-entity-kind-form]");
  if (!form) return;
  const select = form.querySelector("[data-entity-kind-select]");
  const groups = [...form.querySelectorAll("[data-entity-kind-fields]")];
  const update = () => {
    for (const group of groups) {
      const active = group.dataset.entityKindFields === select.value;
      group.hidden = !active;
      for (const field of group.querySelectorAll("input, textarea, select")) field.disabled = !active;
    }
  };
  select.addEventListener("change", update);
  update();
})();
