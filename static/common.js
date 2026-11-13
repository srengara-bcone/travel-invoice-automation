// Click-to-copy for any element with a data-copy attribute, with a small "Copied" toast.
// Dragging to select text still works: a click that ends a selection is not treated as a copy.
(function () {
  let timer;

  function toast(msg) {
    let t = document.getElementById('toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'toast';
      t.setAttribute('role', 'status');
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(timer);
    timer = setTimeout(() => t.classList.remove('show'), 1500);
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const a = document.createElement('textarea');
      a.value = text;
      document.body.appendChild(a);
      a.select();
      try { document.execCommand('copy'); } catch {}
      a.remove();
    }
  }

  window.copyToClipboard = copyToClipboard;
  window.toast = toast;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-copy]');
    if (!el || String(window.getSelection()).length) return;
    copyToClipboard(el.dataset.copy).then(() => toast('Copied ' + el.dataset.copy));
  });
})();
