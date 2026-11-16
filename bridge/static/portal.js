document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const open = button.dataset.openDialog;
  const close = button.dataset.closeDialog;
  if (open) document.getElementById(open)?.showModal();
  if (close) document.getElementById(close)?.close();
});
