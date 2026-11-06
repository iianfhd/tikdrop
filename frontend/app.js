const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const urlInput = $('#videoUrl'), urlField = $('#urlField'), pasteButton = $('#pasteButton'), clearButton = $('#clearButton'),
  fieldHint = $('#fieldHint'), downloadButton = $('#downloadButton'), buttonText = $('.button-text'), message = $('#message'),
  qualityRow = $('#qualityRow'), formatHint = $('#formatHint'), qualityHint = $('#qualityHint'), themeButton = $('#themeButton'),
  installButton = $('#installButton'), installHelp = $('#installHelp'), logoutButton = $('#logoutButton'),
  formatButtons = $$('.format-option'), qualityButtons = $$('.quality-option'),
  vf = $('#vf'), screenEl = $('#screen'), videoThumbnail = $('#videoThumbnail'), hudTag = $('#hudTag'), hudUser = $('#hudUser'), hudMeta = $('#hudMeta'), tray = $('#tray');
let selectedFormat = 'video', selectedQuality = 'best', state = 'idle', user = '';
let previewTimer = 0, previewController = null, previewGeneration = 0;
let deferredInstallPrompt = null, controlledBeforeUpdate = Boolean(navigator.serviceWorker?.controller), reloadAfterDownload = false;

/* ---------- Layar pratinjau: resolusi mengikuti kualitas yang dipilih ---------- */
const cv = $('#cv'), cx = cv.getContext('2d'), off = document.createElement('canvas'), ox = off.getContext('2d');
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const COLS = { best: 0, 1080: 110, 720: 64, 480: 36 }, RATE = { idle: .18, ready: 1, busy: 1, done: 1, error: 0 };
let W = 0, H = 0, cssW = 264, lc = 0, t = 0, rate = .18, am = 0, last = 0, running = false;

function size() {
  const d = Math.min(devicePixelRatio || 1, 2);
  cssW = cv.clientWidth || 264; W = cv.width = Math.round(cssW * d); H = cv.height = Math.round(cv.clientHeight * d || W * 16 / 9);
}
const coarse = () => Math.log(12 * cssW / 264);
const target = () => selectedFormat === 'audio' || !COLS[selectedQuality] ? Math.log(W) : Math.log(COLS[selectedQuality] * cssW / 264);

function scene(c, w, h, T, a) {
  c.globalAlpha = 1; c.fillStyle = '#0a0e2a'; c.fillRect(0, 0, w, h);
  if (a < 1) {
    c.globalAlpha = 1 - a;
    c.save(); c.translate(w * .5, h * .64); c.rotate(-.5 + Math.sin(T * .4) * .14); c.fillStyle = '#4a5bff';
    c.beginPath(); c.roundRect(-w * .7, -w * .15 + Math.sin(T * .8) * w * .05, w * 1.4, w * .3, w * .15); c.fill(); c.restore();
    const X = w * (.5 + .12 * Math.sin(T * .6)), Y = h * (.4 + .04 * Math.cos(T * .5));
    c.strokeStyle = 'rgba(255,255,255,.4)'; c.lineWidth = Math.max(1, w * .006);
    for (let i = 1; i < 4; i++) { c.beginPath(); c.arc(X, Y, w * (.3 + i * .09), 0, 7); c.stroke(); }
    c.fillStyle = '#ff6a3d'; c.beginPath(); c.arc(X, Y, w * .3, 0, 7); c.fill();
    c.strokeStyle = '#ffc247'; c.lineWidth = w * .045; c.lineCap = 'round'; c.beginPath(); c.arc(X, Y, w * .4, T, T + 2.2); c.stroke();
    c.fillStyle = '#38e1d0'; c.beginPath(); c.arc(w * (.2 + .05 * Math.sin(T)), h * .82, w * .05, 0, 7); c.fill();
  }
  if (a > 0) {
    c.globalAlpha = a; const n = 13, bw = w / (n * 1.6);
    for (let i = 0; i < n; i++) {
      const bh = h * (.25 + .2 * Math.sin(T * 3 + i * .7) + .15 * Math.sin(T * 5.3 + i * 1.9)) * .9;
      c.fillStyle = i % 4 ? '#ff6a3d' : '#ffc247'; c.beginPath(); c.roundRect(w * .1 + i * w * .8 / (n - 1) - bw / 2, h * .5 - bh / 2, bw, bh, bw / 2); c.fill();
    }
  }
  c.globalAlpha = 1;
}
function paint() {
  const cols = Math.exp(lc);
  if (cols >= W * .92) return scene(cx, W, H, t, am);
  const cw = Math.max(2, Math.round(cols)), ch = Math.round(cw * H / W);
  off.width = cw; off.height = ch; scene(ox, cw, ch, t, am);
  cx.imageSmoothingEnabled = false; cx.drawImage(off, 0, 0, cw, ch, 0, 0, W, H);
}
function frame(now) {
  const dt = Math.min((now - last) / 1000, .1); last = now;
  rate += (RATE[state] - rate) * (1 - Math.exp(-dt * 3)); t += dt * rate;
  am += ((selectedFormat === 'audio' ? 1 : 0) - am) * (1 - Math.exp(-dt * 8));
  lc += (target() - lc) * (1 - Math.exp(-dt * (state === 'busy' ? 2.2 : 4)));
  paint();
  if (!document.hidden) requestAnimationFrame(frame); else running = false;
}
function go() { if (running || RM) return; running = true; last = performance.now(); requestAnimationFrame(frame); }
function sync() { if (RM) { lc = target(); am = selectedFormat === 'audio' ? 1 : 0; paint(); } else go(); }
document.addEventListener('visibilitychange', () => !document.hidden && go());
addEventListener('resize', () => { size(); sync(); });
size(); lc = coarse(); am = 0; sync();

const label = () => selectedFormat === 'audio' ? 'MP3' : `MP4 · ${selectedQuality === 'best' ? 'Auto' : selectedQuality + 'p'}`;
function who(u) { try { const p = new URL(u), m = p.pathname.match(/@([^/]+)/); return m ? '@' + m[1] : p.hostname.replace(/^www\./, '') + p.pathname.slice(0, 12); } catch { return ''; } }
function setState(s, note) {
  const prev = state; state = s; vf.dataset.state = s; hudTag.textContent = label();
  const T = {
    idle: ['Belum ada link', 'Tempel link TikTok untuk mulai', 'Menunggu link'],
    ready: [user, 'Video siap diproses', `Siap diunduh sebagai ${label()}`],
    busy: [user, 'Memproses…', 'Menyiapkan file…'],
    done: [user, 'Tersimpan', note || 'File siap diunduh'],
    error: [user, 'Gagal diproses', 'Tidak bisa memproses']
  }[s];
  [hudUser.textContent, hudMeta.textContent, tray.textContent] = T;
  if (s === 'busy' && prev !== 'busy') lc = coarse();
  if (s === 'done') { screenEl.classList.remove('drop'); void screenEl.offsetWidth; screenEl.classList.add('drop'); }
  sync();
}

/* ---------- Form ---------- */
function setMessage(text = '', type = '') { message.textContent = text; message.className = `message ${type}`.trim(); }
function clearValidation() {
  urlField.classList.remove('invalid'); fieldHint.classList.remove('error');
  fieldHint.textContent = 'Tempel tautan video TikTok yang ingin kamu simpan.';
}
function flagInvalid(text) {
  urlField.classList.remove('invalid'); void urlField.offsetWidth; urlField.classList.add('invalid');
  fieldHint.textContent = text; fieldHint.classList.add('error'); urlInput.focus();
}
function isTikTokUrl(value) {
  try {
    const host = new URL(value).hostname.toLowerCase().replace(/^www\./, '');
    return ['http:', 'https:'].includes(new URL(value).protocol) && (host === 'tiktok.com' || host.endsWith('.tiktok.com'));
  } catch { return false; }
}
function clearPreview() {
  screenEl.classList.remove('has-thumbnail');
  videoThumbnail.onload = null; videoThumbnail.onerror = null;
  videoThumbnail.removeAttribute('src');
}
function schedulePreview(value) {
  const generation = ++previewGeneration;
  clearTimeout(previewTimer);
  previewController?.abort(); previewController = null;
  clearPreview();
  if (!value || !isTikTokUrl(value)) return;
  previewTimer = setTimeout(async () => {
    const controller = new AbortController(); previewController = controller;
    if (state !== 'busy') fieldHint.textContent = 'Mengambil pratinjau video…';
    try {
      const response = await fetch('/api/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: value }), signal: controller.signal
      });
      const data = await response.json().catch(() => ({}));
      if (generation !== previewGeneration || urlInput.value.trim() !== value) return;
      if (response.status === 401) { location.replace('/login'); return; }
      if (!response.ok) throw new Error(data.detail || 'Pratinjau tidak tersedia.');
      if (data.title) hudUser.textContent = data.title;
      const attribution = [data.author, Number.isFinite(data.duration) ? `${Math.round(data.duration)} dtk` : ''].filter(Boolean).join(' · ');
      if (attribution) hudMeta.textContent = attribution;
      const urls = [data.thumbnail_url, ...(Array.isArray(data.thumbnail_fallbacks) ? data.thumbnail_fallbacks : [])].filter(Boolean);
      let index = 0;
      const tryNext = () => {
        if (generation !== previewGeneration || index >= urls.length) {
          clearPreview();
          if (generation === previewGeneration && state !== 'busy') fieldHint.textContent = 'Info video tersedia, tetapi thumbnail tidak bisa dimuat.';
          return;
        }
        videoThumbnail.onload = () => {
          if (generation === previewGeneration) screenEl.classList.add('has-thumbnail');
        };
        videoThumbnail.onerror = () => { index++; tryNext(); };
        videoThumbnail.src = urls[index]; index++;
      };
      if (urls.length) tryNext();
      else if (state !== 'busy') fieldHint.textContent = 'Info video tersedia, tetapi thumbnail tidak disediakan.';
    } catch (error) {
      if (error.name === 'AbortError' || generation !== previewGeneration) return;
      if (state !== 'busy') fieldHint.textContent = 'Pratinjau tidak tersedia; Anda tetap bisa mencoba download.';
    }
  }, 450);
}
function updateInputState() {
  const v = urlInput.value.trim();
  clearButton.classList.toggle('visible', Boolean(v)); clearValidation();
  if (message.textContent) setMessage();
  schedulePreview(v);
  if (state === 'busy') return;
  user = isTikTokUrl(v) ? who(v) : ''; setState(user ? 'ready' : 'idle');
}
function setFormat(format) {
  selectedFormat = format; const isAudio = format === 'audio';
  formatButtons.forEach(b => { const on = b.dataset.format === format; b.setAttribute('aria-pressed', String(on)); });
  $('#formatSeg').style.setProperty('--i', isAudio ? 1 : 0);
  qualityRow.classList.toggle('audio-mode', isAudio); qualityRow.inert = isAudio;
  formatHint.textContent = isAudio ? 'Audio MP3' : 'Video MP4';
  buttonText.textContent = isAudio ? 'Download audio' : 'Download video';
  setState(state);
}
function setQuality(quality) {
  selectedQuality = quality;
  qualityButtons.forEach((b, i) => {
    const on = b.dataset.quality === quality; b.setAttribute('aria-pressed', String(on));
    if (on) $('#qualitySeg').style.setProperty('--i', i);
  });
  qualityHint.textContent = quality === 'best' ? 'Mengambil kualitas sumber terbaik' : `Maks. ${quality}p · tidak menaikkan sumber yang lebih rendah`;
  setState(state);
}
urlInput.addEventListener('input', updateInputState);
urlInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') downloadButton.click();
  if (e.key === 'Escape') { urlInput.value = ''; updateInputState(); }
});
clearButton.addEventListener('click', () => { urlInput.value = ''; updateInputState(); urlInput.focus(); });
pasteButton.addEventListener('click', async () => {
  try {
    if (!navigator.clipboard?.readText) throw new Error();
    const text = (await navigator.clipboard.readText()).trim();
    if (!text) return setMessage('Clipboard kosong. Salin link TikTok terlebih dahulu.', 'error');
    urlInput.value = text; updateInputState(); urlInput.focus();
    if (!isTikTokUrl(text)) { flagInvalid('Tautan sudah ditempel, tetapi bukan link TikTok yang dikenali.'); return setMessage('Periksa lagi link yang ditempel.', 'error'); }
  } catch { urlInput.focus(); setMessage('Clipboard tidak diizinkan browser. Tekan lama kolom link lalu pilih Tempel.', 'error'); }
});
formatButtons.forEach(b => b.addEventListener('click', () => setFormat(b.dataset.format)));
qualityButtons.forEach(b => b.addEventListener('click', () => setQuality(b.dataset.quality)));

/* ---------- Tema (dengan transisi melingkar dari tombol) ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('meta[name="theme-color"]').content = theme === 'light' ? '#edeffa' : '#0d1030';
  themeButton.setAttribute('aria-label', theme === 'light' ? 'Ganti ke tema gelap' : 'Ganti ke tema terang');
}
applyTheme(document.documentElement.dataset.theme || 'dark');
themeButton.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  try { localStorage.setItem('tikdrop-theme', next); } catch { /* tema tetap berubah untuk sesi ini */ }
  if (!document.startViewTransition || RM) return applyTheme(next);
  const r = themeButton.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  const rad = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  document.startViewTransition(() => applyTheme(next)).ready.then(() => document.documentElement.animate(
    { clipPath: [`circle(0 at ${x}px ${y}px)`, `circle(${rad}px at ${x}px ${y}px)`] },
    { duration: 650, easing: 'cubic-bezier(.4,0,.2,1)', pseudoElement: '::view-transition-new(root)' }));
});

/* ---------- PWA install and safe service worker updates ---------- */
function isStandalone() {
  return matchMedia('(display-mode: standalone)').matches || navigator.standalone === true ||
    document.referrer.startsWith('android-app://');
}
function isIOSSafari() {
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  return ios && /Safari/.test(ua) && !/(CriOS|FxiOS|EdgiOS|OPiOS|DuckDuckGo)/.test(ua);
}
function syncInstallControl() {
  const canShow = !isStandalone() && (Boolean(deferredInstallPrompt) || isIOSSafari());
  installButton.hidden = !canShow;
  installButton.title = deferredInstallPrompt ? 'Install TikDrop' : 'Petunjuk install TikDrop';
  installButton.setAttribute('aria-label', installButton.title);
}
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  deferredInstallPrompt = event;
  syncInstallControl();
});
window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  syncInstallControl();
});
installButton.addEventListener('click', async () => {
  if (deferredInstallPrompt) {
    const promptEvent = deferredInstallPrompt;
    deferredInstallPrompt = null;
    try {
      await promptEvent.prompt();
      await promptEvent.userChoice;
    } catch { /* Prompt may be dismissed or unavailable in this browser session. */ }
    finally { syncInstallControl(); }
  } else if (isIOSSafari() && !isStandalone()) {
    installHelp.showModal();
  }
});
const standaloneMode = matchMedia('(display-mode: standalone)');
standaloneMode.addEventListener?.('change', syncInstallControl);
syncInstallControl();

if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!controlledBeforeUpdate) {
      controlledBeforeUpdate = true;
      return;
    }
    if (state === 'busy') reloadAfterDownload = true;
    else location.reload();
  });
  navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' }).then(registration => {
    registration.update().catch(() => {});
  }).catch(() => {
    // PWA support is optional; the online download app remains usable without it.
  });
}
function reloadAfterPendingWorkerUpdate() {
  if (!reloadAfterDownload) return;
  reloadAfterDownload = false;
  setTimeout(() => location.reload(), 1200);
}

async function clearPwaShellCache() {
  const worker = navigator.serviceWorker?.controller;
  if (!worker || !window.MessageChannel) return;
  await new Promise(resolve => {
    const channel = new MessageChannel();
    const timeout = setTimeout(resolve, 1500);
    channel.port1.onmessage = () => { clearTimeout(timeout); resolve(); };
    worker.postMessage({ type: 'CLEAR_TIKDROP_CACHES' }, [channel.port2]);
  });
}
logoutButton.addEventListener('click', async () => {
  logoutButton.disabled = true;
  try {
    const response = await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    if (!response.ok && response.status !== 401) throw new Error('Logout gagal. Coba lagi.');
    await clearPwaShellCache();
    location.replace('/login');
  } catch (error) {
    logoutButton.disabled = false;
    setMessage(error instanceof TypeError ? 'Server tidak dapat dijangkau.' : error.message, 'error');
  }
});

/* ---------- Download ---------- */
function setLoading(loading) {
  downloadButton.disabled = loading; downloadButton.classList.toggle('loading', loading);
  buttonText.textContent = loading ? 'Sedang memproses...' : selectedFormat === 'audio' ? 'Download audio' : 'Download video';
  if (!loading) reloadAfterPendingWorkerUpdate();
}
function triggerDownload(downloadUrl, filename) {
  const a = document.createElement('a'); a.href = downloadUrl; if (filename) a.download = filename; a.rel = 'noopener';
  document.body.appendChild(a); a.click(); a.remove();
}
downloadButton.addEventListener('click', async () => {
  const url = urlInput.value.trim();
  if (!url) return flagInvalid('Link belum diisi. Tempel link TikTok terlebih dahulu.');
  if (!isTikTokUrl(url)) return flagInvalid('Link tidak dikenali. Gunakan tautan dari tiktok.com.');

  user = who(url); setLoading(true); setState('busy'); setMessage('Menghubungkan ke layanan download...');
  const started = performance.now(); let result, failure;
  try {
    // Kontrak backend: POST /api/download { url, format: "video"|"audio", quality: "best"|"1080"|"720"|"480" }
    // Response metadata comes from ffprobe on the completed output file.
    const response = await fetch('/api/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format: selectedFormat, quality: selectedFormat === 'audio' ? 'best' : selectedQuality })
    });
    if (response.status === 401) {
      location.replace('/login');
      throw new Error('Sesi berakhir. Masuk kembali untuk melanjutkan.');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `Server merespons dengan status ${response.status}.`);
    if (!data.download_url) throw new Error('Server belum mengirim file hasil download.');
    result = data;
  } catch (error) { failure = error; }
  await new Promise(r => setTimeout(r, Math.max(0, 1100 - (performance.now() - started)))); // beri waktu animasi terbaca
  setLoading(false);
  if (failure) {
    setState('error');
    return setMessage(failure instanceof TypeError ? 'Backend belum tersambung. Tampilan siap, tetapi endpoint /api/download belum aktif.' : failure.message, 'error');
  }
  setState('done', result.title || result.filename);
  const requested = result.requested_quality ? `Diminta ${result.requested_quality}` : '';
  const sourceSize = result.source_width && result.source_height ? `${result.source_width}x${result.source_height}` : result.source_resolution || '';
  const sourceNote = sourceSize ? `Sumber ${sourceSize}` : '';
  const outputSize = result.actual_width && result.actual_height ? `${result.actual_width}x${result.actual_height}` : result.resolution || '';
  const outputNote = outputSize ? `Hasil ${outputSize}` : '';
  const sizeNote = Number.isFinite(result.filesize) ? `${(result.filesize / 1048576).toFixed(2)} MB` : '';
  const codecs = [result.video_codec || result.actual_video_codec, result.audio_codec || result.actual_audio_codec].filter(Boolean).join(' + ');
  const fileFormat = result.file_extension ? `Format ${result.file_extension.toUpperCase()}` : '';
  const qualityInfo = [requested, sourceNote, outputNote, fileFormat, sizeNote, codecs].filter(Boolean).join(' | ');
  const warning = result.source_below_requested
    ? `Sumber hanya ${sourceSize}; kualitas maksimum yang tersedia ${sourceSize}.`
    : '';
  setMessage([result.title || 'File siap diunduh', warning, qualityInfo].filter(Boolean).join(' | '), 'success');
  triggerDownload(result.download_url, result.filename || 'tikdrop-file');
});
setState('idle');
