/* Refresh once OCR has finished so the extracted fields, not only status, appear. */
(() => {
  const marker = document.querySelector('[data-kad-pending]');
  if (!marker) return;
  const initial = marker.dataset.kadPending;
  document.body.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target.id !== 'task-status') return;
    const state = event.detail.target.querySelector('[data-task-state]')?.dataset.taskState;
    if (state && state !== initial && !['new', 'recognizing'].includes(state)) location.reload();
  });
})();
