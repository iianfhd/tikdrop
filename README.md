# TikDrop v1.6 — PWA frontend + FastAPI backend

TikDrop adalah proyek lokal berbasis HTML/CSS/JavaScript dan Python FastAPI. Frontend v3 yang kamu kirim dipertahankan dan disajikan langsung oleh FastAPI sehingga request `/api/download` memakai origin yang sama.

## Kebutuhan

- Windows 10/11
- Python 3.10 atau lebih baru
- Koneksi internet saat instalasi dan saat mengambil video
- FFmpeg + FFprobe di PATH untuk ekstraksi MP3 dan penggabungan/remux video

## Instalasi pertama

1. Ekstrak folder proyek.
2. Pastikan Python terpasang dan bisa dipanggil dari terminal (`python --version`).
3. Jalankan `setup.bat` satu kali. Script ini membuat virtual environment dan memasang dependensi.
4. Pasang FFmpeg. TikDrop akan mencoba menemukan FFmpeg WinGet Gyan secara otomatis meskipun PATH terminal lama belum diperbarui.
5. (Opsional) Salin `.env.example` menjadi `.env` untuk menyesuaikan batas penggunaan. TikDrop tidak memerlukan password atau akun.
6. Tutup server TikDrop yang sedang berjalan (`CTRL+C`), lalu jalankan `run.bat` dari terminal baru.
7. Buka `http://127.0.0.1:8000`; halaman utama TikDrop langsung tersedia.

## Membuka dari HP di Wi-Fi yang sama

1. Jalankan `run-lan.bat` di laptop.
2. Di Command Prompt laptop, jalankan `ipconfig` dan cari IPv4 Address adapter Wi-Fi (contoh `192.168.1.10`).
3. Buka `http://192.168.1.10:8000` di browser HP yang terhubung ke Wi-Fi yang sama.
4. Jika Windows Firewall meminta izin, izinkan hanya pada jaringan Private.

`run-lan.bat` membuka server ke jaringan lokal. Jangan gunakan jaringan publik dan jangan meneruskan port ini langsung ke internet.

## Akses publik dan Cloudflare Quick Tunnel

TikDrop tidak meminta akun, password, atau cookie sesi. Halaman utama, preview, download, file hasil, dan health dapat diakses langsung. Untuk membatasi penggunaan, preview dibatasi 30 permintaan per menit per IP; pembuatan download 5 permintaan per jam per IP; dan pengambilan file 30 permintaan per jam per IP secara default. Dua batas download dapat diubah dengan `TIKDROP_MAX_DOWNLOADS_PER_IP_PER_HOUR` dan `TIKDROP_MAX_FILE_SERVES_PER_IP_PER_HOUR`.

Penghitung rate limit berada di memori proses, jadi restart mengosongkan jendela limit dan deployment multi-worker perlu rate limiter bersama di proxy atau Redis. TikDrop mengambil alamat client dari peer koneksi dan hanya memakai `CF-Connecting-IP` saat koneksi proxy lokal; jangan membuka origin di belakang proxy yang tidak dipercaya.

Untuk uji Quick Tunnel, jalankan `cloudflared tunnel --url http://localhost:8000`, lalu buka URL HTTPS `trycloudflare.com` yang dicetak `cloudflared`. Siapa pun yang memiliki URL publik dapat menggunakan server, dan download memakai resource mesin yang menjalankannya. Quick Tunnel ditujukan untuk pengembangan dan pengujian, bukan hosting produksi; [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) menjelaskan batasnya.

## Install TikDrop sebagai PWA

TikDrop menyediakan manifest dan service worker untuk memasang aplikasi serta membuka shell antarmuka saat offline. Fitur download tetap memerlukan backend dan koneksi internet.

- **Android:** buka TikDrop lewat HTTPS di Chrome atau browser berbasis Chromium, lalu gunakan tombol Install TikDrop jika tersedia atau menu browser **Install app / Add to Home screen**.
- **iPhone/iPad:** buka TikDrop lewat HTTPS di Safari, ketuk **Bagikan**, pilih **Tambahkan ke Layar Utama**, lalu ketuk Tambahkan. Tombol petunjuk TikDrop menampilkan langkah tersebut.
- **HTTPS:** service worker dan prompt instalasi memerlukan secure context; gunakan HTTPS dengan sertifikat tepercaya atau Quick Tunnel.
- Host server dapat diatur lewat `TIKDROP_HOST` dan port lewat `TIKDROP_PORT`. `run.bat` memakai `127.0.0.1`; `run-lan.bat` memakai `0.0.0.0` secara default. Untuk mengubahnya dari Command Prompt, jalankan `set TIKDROP_HOST=0.0.0.0` dan `set TIKDROP_PORT=8000`, lalu mulai `run-lan.bat`.
- Service worker hanya menyimpan shell frontend dan ikon lokal. Request API, URL thumbnail TikTok, serta file MP4/MP3 tidak disimpan di cache.

## API

Endpoint API dan file hasil tidak memerlukan login. Dokumentasi interaktif FastAPI dan skema OpenAPI dinonaktifkan.

### `GET /api/health`

Memeriksa backend, FFmpeg, FFprobe, dan frontend.

### `POST /api/download`

Request JSON:

```json
{
  "url": "https://www.tiktok.com/@username/video/1234567890",
  "format": "video",
  "quality": "best"
}
```

- `format`: `video` atau `audio`
- `quality`: `best`, `1080`, `720`, atau `480`. `best` mengambil sumber terbaik. Pilihan angka menetapkan batas resolusi output dan FFmpeg akan mengecilkan video bila sumber lebih tinggi. Video sumber yang lebih rendah tidak di-upscale.
- Format audio otomatis menggunakan kualitas `best`.

Response sukses:

```json
{
  "download_url": "/api/files/0123456789abcdef0123456789abcdef.mp4",
  "filename": "0123456789abcdef0123456789abcdef.mp4",
  "title": "Judul video"
}
```

File disimpan sementara dan dibersihkan otomatis setelah `TIKDROP_FILE_TTL_HOURS` (default 6 jam).

## Pengaturan opsional

Salin `.env.example` menjadi `.env`, lalu atur:

- `TIKDROP_MAX_FILE_SIZE_MB` — batas ukuran file (default 500 MB)
- `TIKDROP_MAX_DURATION_SECONDS` — batas durasi video (default 600 detik; MP3 tidak dibatasi pengaturan ini)
- `TIKDROP_FILE_TTL_HOURS` — umur file hasil (default 6 jam)
- `TIKDROP_MAX_CONCURRENT_DOWNLOADS` — jumlah download bersamaan (default 2)
- `TIKDROP_MAX_DOWNLOADS_PER_IP_PER_HOUR` — jumlah permintaan download per IP per jam (default 5)
- `TIKDROP_MAX_FILE_SERVES_PER_IP_PER_HOUR` — jumlah pengambilan file per IP per jam (default 30)
- `TIKDROP_SOCKET_TIMEOUT` — timeout koneksi (default 30 detik)

## Catatan kompatibilitas dan batasan

- Backend memakai yt-dlp untuk memproses video TikTok yang dapat diakses secara publik. Perubahan TikTok dapat membuat ekstraksi sementara gagal; perbarui yt-dlp dengan mengaktifkan virtual environment lalu menjalankan `python -m pip install -U yt-dlp`.
- Kualitas akhir bergantung pada resolusi dan bitrate video sumber. Angka 1080p tidak dapat membuat video sumber yang buram menjadi lebih tajam. TikDrop tidak melakukan upscale; bila sumber lebih rendah dari pilihan, resolusi sumber dipertahankan. Untuk 720p/480p, FFmpeg menurunkan resolusi sumber yang lebih tinggi sehingga tidak bergantung pada adanya rendition tepat 720p/480p dari TikTok.
- TikDrop tidak dapat menjamin video tanpa watermark. Hasil bergantung pada format yang secara sah disediakan dan dapat diakses dari sumber.
- Video privat, video yang memerlukan login, atau konten yang aksesnya dibatasi tidak didukung. Jangan gunakan untuk mengunduh atau menyebarkan konten tanpa izin pemiliknya.
- Audio MP3, penggabungan/remux video, dan penyesuaian resolusi memerlukan FFmpeg. TikDrop mencoba mendeteksi instalasi Gyan FFmpeg dari WinGet secara otomatis saat backend dimulai.
- Karena aplikasi bersifat publik, batas rate dan resource diterapkan per IP dan global. Jika dijalankan di belakang proxy, batasi akses origin ke proxy tepercaya agar alamat client yang dipakai limiter tidak dapat dipalsukan.

## Struktur proyek

```text
tikdrop/
├── frontend/              # Frontend v3 asli
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── manifest.json
│   ├── sw.js
│   └── icons/              # Ikon PWA dan Apple Touch Icon lokal
├── backend/
│   ├── main.py            # API FastAPI + file response
│   ├── downloader.py      # Integrasi yt-dlp + FFmpeg
│   ├── schemas.py         # Validasi request
│   └── config.py          # Konfigurasi dan folder
├── downloads/             # Hasil download sementara
├── .tmp/                  # Proses sementara
├── requirements.txt
├── setup.bat
├── run.bat
└── run-lan.bat
```
