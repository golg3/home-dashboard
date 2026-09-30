# Home Dashboard

Docker üzerinde çalışan mobil uyumlu ev sunucusu, Windows PC ve yayın izleme/kontrol paneli.

## Özellikler

- Windows PC CPU / RAM / GPU / VRAM / sıcaklık / network takibi
- Linux Docker host sistem metrikleri
- Docker container durumları
- OBS Studio bağlantısı ve kontrolü
- OBS ses kaynakları ve canlı ses metreleri
- OBS sahne kontrolü ve önizleme
- YouTube canlı sohbet entegrasyonu
- Kick canlı sohbet entegrasyonu
- Hava durumu
- Mobil uyumlu PWA arayüzü
- Web tabanlı ayarlar

## 1. Kurulum

Projeyi klonladıktan sonra örnek environment dosyasını kopyalayın:

    cp .env.example .env

`.env` dosyasındaki gerekli değerleri düzenleyin:

    AGENT_TOKEN=CHANGE-THIS-LONG-TOKEN
    YOUTUBE_CLIENT_ID=
    YOUTUBE_CLIENT_SECRET=
    YOUTUBE_REDIRECT_URI=
    OBS_PASSWORD=

Güvenli bir Agent Token oluşturmak için:

    openssl rand -hex 32

Ardından sistemi başlatın:

    docker compose up -d --build

Dashboard:

    http://SUNUCU_IP:8088

## 2. Windows Agent

Windows bilgisayarda:

    windows-agent/agent.ps1

dosyasını düzenleyin.

`$Server` değerini Docker sunucusuna yönlendirin:

    $Server = "http://SUNUCU_IP:8088/api/metrics"

`$Token` değeri `.env` dosyasındaki `AGENT_TOKEN` ile aynı olmalıdır:

    $Token = "AGENT_TOKEN_BURAYA"

PowerShell üzerinden çalıştırmak için:

    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
    .\agent.ps1

NVIDIA GPU metrikleri için `nvidia-smi` kullanılmaktadır.

## 3. OBS Studio

OBS WebSocket bağlantısı dashboard tarafından kullanılır.

Varsayılan port:

    4455

OBS bağlantı bilgileri dashboard Ayarlar bölümünden yapılandırılabilir.

## 4. YouTube

YouTube canlı sohbet özelliği için Google Cloud üzerinde YouTube Data API v3 ve OAuth yapılandırması gerekir.

OAuth bilgileri `.env` veya dashboard Ayarlar bölümünden yapılandırılabilir.

OAuth token dosyaları `youtube-data/` altında tutulur ve Git'e dahil edilmez.

## 5. Mobil / PWA

Dashboard mobil tarayıcı üzerinden:

    http://SUNUCU_IP:8088

adresinden kullanılabilir.

HTTPS reverse proxy üzerinden yayınlandığında desteklenen tarayıcılarda PWA olarak ana ekrana kurulabilir.

## Güvenlik

Aşağıdaki dosyalar Git repository'sine dahil edilmez:

- `.env`
- `youtube-data/`
- `data/settings.json`
- backup ve geçici dosyalar

Gerçek token, parola, OAuth Client Secret veya diğer credential bilgilerini repository'ye commit etmeyin.
