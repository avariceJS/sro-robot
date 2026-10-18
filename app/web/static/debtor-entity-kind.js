(() => {
  const form = document.querySelector("[data-entity-kind-form]");
  if (!form) return;
  const select = form.querySelector("[data-entity-kind-select]");
  const groups = [...form.querySelectorAll("[data-entity-kind-fields]")];
  let activeKind = select.value;

  const hasChanges = (control) => {
    if (control.type === "checkbox" || control.type === "radio") {
      return control.checked !== control.defaultChecked;
    }
    if (control.tagName === "SELECT") {
      return [...control.options].some((option) => option.selected !== option.defaultSelected);
    }
    return String(control.value) !== String(control.defaultValue ?? "");
  };

  const update = () => {
    for (const group of groups) {
      const active = group.dataset.entityKindFields === select.value;
      group.hidden = !active;
      for (const field of group.querySelectorAll("input, textarea, select")) field.disabled = !active;
    }
  };

  select.addEventListener("change", () => {
    const previousGroup = groups.find((group) => group.dataset.entityKindFields === activeKind);
    const previousFields = [...(previousGroup?.querySelectorAll("input, textarea, select") || [])];
    const pendingValues = form.dataset.entityKindPending === "true"
      && previousFields.some((control) => "value" in control && String(control.value).trim() !== "");
    if (pendingValues || previousFields.some(hasChanges)) {
      const confirmed = window.confirm(
        "Введённые сведения для текущего типа карточки будут очищены. Продолжить?",
      );
      if (!confirmed) {
        select.value = activeKind;
        update();
        return;
      }
      previousFields.forEach((control) => {
        if ("value" in control) control.value = "";
        if ("checked" in control) control.checked = false;
        if (control.tagName === "SELECT") {
          for (const option of control.options) option.selected = option.value === "";
        }
      });
      form.dataset.entityKindPending = "false";
    }
    activeKind = select.value;
    update();
  });
  update();
})();
