const form = document.querySelector('#loginForm');
const passwordField = document.querySelector('#accessPassword');
const loginButton = document.querySelector('#loginButton');
const errorBox = document.querySelector('#loginError');

async function clearSavedAppShell() {
  const worker = navigator.serviceWorker?.controller;
  if (!worker || !window.MessageChannel) return;
  await new Promise(resolve => {
    const channel = new MessageChannel();
    const timeout = setTimeout(resolve, 1500);
    channel.port1.onmessage = () => { clearTimeout(timeout); resolve(); };
    worker.postMessage({ type: 'CLEAR_TIKDROP_CACHES' }, [channel.port2]);
  });
}

void clearSavedAppShell();

form.addEventListener('submit', async event => {
  event.preventDefault();
  errorBox.hidden = true;
  loginButton.disabled = true;
  loginButton.textContent = 'Memeriksa…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: passwordField.value })
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || 'Tidak dapat masuk. Coba lagi.');
    passwordField.value = '';
    await clearSavedAppShell();
    location.replace('/');
  } catch (error) {
    errorBox.textContent = error instanceof TypeError
      ? 'Server tidak dapat dijangkau. Periksa koneksi lalu coba lagi.'
      : error.message;
    errorBox.hidden = false;
    passwordField.value = '';
    passwordField.focus();
  } finally {
    loginButton.disabled = false;
    loginButton.textContent = 'Masuk';
  }
});
