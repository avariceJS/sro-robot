(() => {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-review-suggestion-copy]");
    if (!button) return;

    const field = document.getElementById(button.dataset.reviewSuggestionCopy);
    if (!field || field.disabled || field.readOnly) return;

    field.value = button.dataset.suggestionValue || "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    field.focus();
    button.textContent = "Перенесено — проверьте поле и сохраните изменение";
  });
})();
