from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode
import json
import os
from fastapi.responses import RedirectResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
import asyncio
import math
import threading

from fastapi import WebSocket, WebSocketDisconnect
from obsws_python.subs import Subs

app = FastAPI(title="Home Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

TOKEN = os.getenv("AGENT_TOKEN", "change-me")

# YouTube Live Chat
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REDIRECT_URI = os.getenv("YOUTUBE_REDIRECT_URI", "")
YOUTUBE_TOKEN_FILE = "/app/youtube-data/token.json"

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly"
]


latest = {}
history = deque(maxlen=720)


class Metrics(BaseModel):
    hostname: str
    cpu: float
    ram: float
    ram_used_gb: float = 0
    ram_total_gb: float = 0

    gpu: float = 0
    vram: float = 0
    vram_used_gb: float = 0
    vram_total_gb: float = 0

    cpu_temp: float | None = None
    gpu_temp: float | None = None

    disk_percent: float = 0
    net_down_mbps: float = 0
    net_up_mbps: float = 0

    uptime: str = ""
    gpu_name: str = ""
    timestamp: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/metrics")
def ingest(
    m: Metrics,
    authorization: str | None = Header(default=None)
):
    # Navigator > Ayarlar > Windows Monitor > Agent Token
    # önceliklidir. Ayarlarda token yoksa .env AGENT_TOKEN kullanılır.
    settings = settings_load()
    windows_settings = settings.get("windows_monitor", {})
    active_token = str(
        windows_settings.get("agent_token") or TOKEN
    ).strip()

    if authorization != f"Bearer {active_token}":
        raise HTTPException(401, "invalid token")

    d = m.model_dump()

    # Agent'in kendi saatine/saat dilimine guvenmek yerine
    # metrigin sunucuya ulastigi ani timezone-aware UTC olarak kaydet.
    d["timestamp"] = datetime.now(timezone.utc).isoformat()

    latest.update(d)
    history.append(d)

    return {"ok": True}


@app.get("/api/metrics")
def get_metrics():
    if not latest:
        return {
            "online": False,
            "stale": True
        }

    result = dict(latest)

    timestamp = result.get("timestamp")

    if not timestamp:
        result["online"] = False
        result["stale"] = True
        return result

    try:
        last_seen = datetime.fromisoformat(timestamp)
        # Timestamp timezone bilgisi içeriyorsa aynı timezone ile karşılaştır.
        if last_seen.tzinfo is not None:
            now = datetime.now(timezone.utc)
            last_seen = last_seen.astimezone(timezone.utc)
        else:
            # Eski agent verileri timezone bilgisi içermeyebilir.
            now = datetime.now()

        age_seconds = (
            now - last_seen
        ).total_seconds()

        result["online"] = age_seconds <= 15
        result["stale"] = age_seconds > 15
        result["age_seconds"] = round(age_seconds, 1)

    except (TypeError, ValueError):
        result["online"] = False
        result["stale"] = True

    return result


@app.get("/api/history")
def get_history():
    return list(history)


# ---------------------------------------------------------
# WEATHER
# ---------------------------------------------------------

WEATHER_CODES = {
    0: "Açık",
    1: "Çoğunlukla açık",
    2: "Parçalı bulutlu",
    3: "Kapalı",
    45: "Sisli",
    48: "Kırağılı sis",
    51: "Hafif çisenti",
    53: "Çisenti",
    55: "Yoğun çisenti",
    56: "Hafif donan çisenti",
    57: "Donan çisenti",
    61: "Hafif yağmur",
    63: "Yağmur",
    65: "Kuvvetli yağmur",
    66: "Hafif donan yağmur",
    67: "Donan yağmur",
    71: "Hafif kar",
    73: "Kar",
    75: "Yoğun kar",
    77: "Kar taneleri",
    80: "Hafif sağanak",
    81: "Sağanak",
    82: "Kuvvetli sağanak",
    85: "Hafif kar sağanağı",
    86: "Kuvvetli kar sağanağı",
    95: "Gök gürültülü fırtına",
    96: "Dolu ihtimalli fırtına",
    99: "Şiddetli dolulu fırtına"
}


def weather_description(code):
    return WEATHER_CODES.get(code, "Bilinmiyor")


def weather_icon(code, is_day=1):
    if code == 0:
        return "☀️" if is_day else "🌙"

    if code in [1, 2]:
        return "🌤️" if is_day else "☁️"

    if code == 3:
        return "☁️"

    if code in [45, 48]:
        return "🌫️"

    if code in [51, 53, 55, 56, 57]:
        return "🌦️"

    if code in [61, 63, 65, 66, 67, 80, 81, 82]:
        return "🌧️"

    if code in [71, 73, 75, 77, 85, 86]:
        return "🌨️"

    if code in [95, 96, 99]:
        return "⛈️"

    return "🌡️"


@app.get("/api/weather")
def get_weather(
    lat: float = Query(41.0333),
    lon: float = Query(30.3075),
    name: str = Query("Kaynarca, Sakarya")
):
    params = {
        "latitude": lat,
        "longitude": lon,

        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "precipitation",
            "weather_code",
            "surface_pressure",
            "wind_speed_10m",
            "wind_direction_10m",
            "is_day"
        ]),

        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation_probability",
            "weather_code"
        ]),

        "daily": ",".join([
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "sunrise",
            "sunset"
        ]),

        "timezone": "auto",
        "forecast_days": 7
    }

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urlencode(params)
    )

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "HomeDashboard/1.0"
            }
        )

        with urlopen(req, timeout=10) as response:
            raw = json.loads(response.read().decode("utf-8"))

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Hava servisine ulaşılamadı: {str(e)}"
        )

    current = raw.get("current", {})
    hourly = raw.get("hourly", {})
    daily = raw.get("daily", {})

    current_code = current.get("weather_code", 0)
    current_is_day = current.get("is_day", 1)

    hourly_result = []

    times = hourly.get("time", [])

    for i in range(len(times)):
        code = hourly["weather_code"][i]

        hourly_result.append({
            "time": times[i],
            "temperature": hourly["temperature_2m"][i],
            "apparent_temperature":
                hourly["apparent_temperature"][i],
            "precipitation_probability":
                hourly["precipitation_probability"][i],
            "weather_code": code,
            "description": weather_description(code),
            "icon": weather_icon(code, 1)
        })

    daily_result = []

    dates = daily.get("time", [])

    for i in range(len(dates)):
        code = daily["weather_code"][i]

        daily_result.append({
            "date": dates[i],
            "weather_code": code,
            "description": weather_description(code),
            "icon": weather_icon(code, 1),
            "max": daily["temperature_2m_max"][i],
            "min": daily["temperature_2m_min"][i],
            "precipitation_probability":
                daily["precipitation_probability_max"][i],
            "sunrise": daily["sunrise"][i],
            "sunset": daily["sunset"][i]
        })

    return {
        "location": {
            "name": name,
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
            "timezone": raw.get("timezone")
        },

        "current": {
            "temperature": current.get("temperature_2m"),
            "apparent_temperature":
                current.get("apparent_temperature"),
            "humidity":
                current.get("relative_humidity_2m"),
            "precipitation":
                current.get("precipitation"),
            "pressure":
                current.get("surface_pressure"),
            "wind_speed":
                current.get("wind_speed_10m"),
            "wind_direction":
                current.get("wind_direction_10m"),
            "weather_code": current_code,
            "description":
                weather_description(current_code),
            "icon":
                weather_icon(current_code, current_is_day),
            "is_day": current_is_day,
            "time": current.get("time")
        },

        "hourly": hourly_result,
        "daily": daily_result
    }
# ---------------------------------------------------------
# DOCKER MONITORING
# ---------------------------------------------------------

import socket
import urllib.parse
import time


DOCKER_SOCKET = "/var/run/docker.sock"


def docker_request(path):
    """
    Docker Engine API'ye Unix socket üzerinden
    yalnızca GET isteği gönderir.
    """

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        s.settimeout(5)
        s.connect(DOCKER_SOCKET)

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: docker\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )

        s.sendall(request.encode())

        data = b""

        while True:
            chunk = s.recv(65536)

            if not chunk:
                break

            data += chunk

    finally:
        s.close()

    header, body = data.split(b"\r\n\r\n", 1)

    status_line = header.split(b"\r\n", 1)[0]

    if b"200" not in status_line:
        raise RuntimeError(
            status_line.decode(
                errors="ignore"
            )
        )

    # Docker bazen chunked encoding döndürür.
    if b"transfer-encoding: chunked" in header.lower():

        decoded = b""
        rest = body

        while rest:

            line_end = rest.find(b"\r\n")

            if line_end < 0:
                break

            size_line = rest[:line_end]

            try:
                size = int(
                    size_line.split(b";")[0],
                    16
                )
            except:
                break

            if size == 0:
                break

            start = line_end + 2
            end = start + size

            decoded += rest[start:end]

            rest = rest[end + 2:]

        body = decoded

    return json.loads(
        body.decode("utf-8")
    )


def format_bytes(value):

    value = float(value or 0)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    for unit in units:

        if value < 1024:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} PB"


def calculate_cpu(stats):

    try:

        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            -
            stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )

        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            -
            stats["precpu_stats"]["system_cpu_usage"]
        )

        cpu_count = (
            stats["cpu_stats"]
            .get("online_cpus")
            or
            len(
                stats["cpu_stats"]
                ["cpu_usage"]
                .get(
                    "percpu_usage",
                    []
                )
            )
            or 1
        )

        if system_delta > 0 and cpu_delta > 0:

            return (
                cpu_delta /
                system_delta *
                cpu_count *
                100
            )

    except:
        pass

    return 0


@app.get("/api/docker/containers")
def docker_containers():

    def collect_container(c):

        cid = c["Id"]

        name = (
            c.get("Names", ["unknown"])[0]
            .lstrip("/")
        )

        state = c.get(
            "State",
            "unknown"
        )

        status = c.get(
            "Status",
            ""
        )

        image = c.get(
            "Image",
            ""
        )

        health = None
        started_at = ""
        ips = []

        # Inspect bilgileri
        try:
            inspect = docker_request(
                f"/containers/{cid}/json"
            )

            health = (
                inspect
                .get("State", {})
                .get("Health", {})
                .get("Status")
            )

            started_at = (
                inspect
                .get("State", {})
                .get("StartedAt", "")
            )

            networks = (
                inspect
                .get("NetworkSettings", {})
                .get("Networks", {})
            )

            for network_name, network in networks.items():

                ip = network.get("IPAddress")

                if ip:
                    ips.append({
                        "network": network_name,
                        "ip": ip
                    })

        except Exception:
            pass

        # Port bilgileri zaten /containers/json cevabında var.
        ports = []

        for port in c.get("Ports", []):

            private_port = port.get(
                "PrivatePort"
            )

            public_port = port.get(
                "PublicPort"
            )

            protocol = port.get(
                "Type",
                "tcp"
            )

            if public_port:
                ports.append(
                    f"{public_port}:{private_port}/{protocol}"
                )
            else:
                ports.append(
                    f"{private_port}/{protocol}"
                )

        cpu_percent = 0
        memory_usage = 0
        memory_limit = 0
        memory_percent = 0
        net_rx = 0
        net_tx = 0

        # Stats sadece çalışan container için alınır.
        if state == "running":

            try:
                stats = docker_request(
                    f"/containers/{cid}/stats?stream=false"
                )

                cpu_percent = calculate_cpu(
                    stats
                )

                memory_stats = stats.get(
                    "memory_stats",
                    {}
                )

                memory_usage = memory_stats.get(
                    "usage",
                    0
                )

                memory_limit = memory_stats.get(
                    "limit",
                    0
                )

                if memory_limit:
                    memory_percent = (
                        memory_usage /
                        memory_limit *
                        100
                    )

                for net in (
                    stats
                    .get("networks", {})
                    .values()
                ):
                    net_rx += net.get(
                        "rx_bytes",
                        0
                    )

                    net_tx += net.get(
                        "tx_bytes",
                        0
                    )

            except Exception:
                pass

        return {
            "id": cid[:12],
            "name": name,
            "image": image,
            "state": state,
            "status": status,
            "health": health,
            "started_at": started_at,

            "cpu": round(
                cpu_percent,
                2
            ),

            "memory_usage":
                memory_usage,

            "memory_limit":
                memory_limit,

            "memory_percent": round(
                memory_percent,
                1
            ),

            "memory_usage_text":
                format_bytes(
                    memory_usage
                ),

            "memory_limit_text":
                format_bytes(
                    memory_limit
                ),

            "network_rx":
                net_rx,

            "network_tx":
                net_tx,

            "network_rx_text":
                format_bytes(
                    net_rx
                ),

            "network_tx_text":
                format_bytes(
                    net_tx
                ),

            "ips": ips,
            "ports": ports
        }

    try:

        containers = docker_request(
            "/containers/json?all=1"
        )

        if not containers:
            return []

        # Inspect ve stats çağrılarını container başına paralel çalıştır.
        # Çok fazla eşzamanlı Docker Engine isteği oluşturmamak için
        # worker sayısını 8 ile sınırla.
        worker_count = min(
            8,
            len(containers)
        )

        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:

            result = list(
                executor.map(
                    collect_container,
                    containers
                )
            )

        result.sort(
            key=lambda x: (
                x["state"] != "running",
                x["name"].lower()
            )
        )

        return result

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=
            f"Docker bilgileri alınamadı: {e}"
        )


@app.get("/api/docker/summary")
def docker_summary():

    try:

        info = docker_request(
            "/info"
        )

        return {

            "name":
                info.get(
                    "Name",
                    ""
                ),

            "docker_version":
                info.get(
                    "ServerVersion",
                    ""
                ),

            "containers":
                info.get(
                    "Containers",
                    0
                ),

            "running":
                info.get(
                    "ContainersRunning",
                    0
                ),

            "paused":
                info.get(
                    "ContainersPaused",
                    0
                ),

            "stopped":
                info.get(
                    "ContainersStopped",
                    0
                ),

            "images":
                info.get(
                    "Images",
                    0
                ),

            "cpus":
                info.get(
                    "NCPU",
                    0
                ),

            "memory":
                info.get(
                    "MemTotal",
                    0
                ),

            "memory_text":
                format_bytes(
                    info.get(
                        "MemTotal",
                        0
                    )
                ),

            "os":
                info.get(
                    "OperatingSystem",
                    ""
                ),

            "kernel":
                info.get(
                    "KernelVersion",
                    ""
                )

        }


    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=
            f"Docker bilgileri alınamadı: {e}"
        )
# ---------------------------------------------------------
# OBS STUDIO
# ---------------------------------------------------------

import obsws_python as obs
import base64
import socket

OBS_HOST = os.getenv("OBS_HOST", "10.29.250.13")
OBS_PORT = int(os.getenv("OBS_PORT", "4455"))
OBS_PASSWORD = os.getenv("OBS_PASSWORD", "")


_obs_req_client = None
_obs_req_lock = threading.RLock()


def obs_connection_settings():
    settings = settings_load()
    obs_cfg = settings.get("obs", {})

    host = str(obs_cfg.get("host") or OBS_HOST).strip()

    try:
        port = int(obs_cfg.get("port") or OBS_PORT)
    except (TypeError, ValueError):
        port = OBS_PORT

    password = obs_cfg.get("password") or OBS_PASSWORD or ""

    return host, port, password


def obs_port_available(timeout=0.25):
    host, port, _ = obs_connection_settings()

    try:
        with socket.create_connection(
            (host, port),
            timeout=timeout
        ):
            return True
    except (OSError, TimeoutError):
        return False


def _obs_connect():
    global _obs_req_client

    if _obs_req_client is None:
        host, port, password = obs_connection_settings()

        if not obs_port_available():
            raise ConnectionError(
                f"OBS çevrimdışı: {host}:{port}"
            )

        _obs_req_client = obs.ReqClient(
            host=host,
            port=port,
            password=password,
            timeout=5
        )

        print(
            f"OBS ReqClient connected: {host}:{port}",
            flush=True
        )

    return _obs_req_client

def obs_reset_client():
    global _obs_req_client

    if _obs_req_client is not None:
        try:
            _obs_req_client.disconnect()
        except Exception:
            pass

    _obs_req_client = None


class OBSClientProxy:
    def __getattr__(self, name):
        def call(*args, **kwargs):
            global _obs_req_client

            with _obs_req_lock:
                # Cached ReqClient mevcut olsa bile OBS sonradan
                # kapanmış olabilir. Bu kontrol try dışında:
                # OBS kapalıysa reconnect yoluna girip ikinci kez
                # bağlantı denemesi yapma.
                if not obs_port_available(timeout=0.20):
                    obs_reset_client()
                    raise ConnectionError("OBS çevrimdışı")

                try:
                    client = _obs_connect()
                    method = getattr(client, name)
                    return method(*args, **kwargs)

                except Exception as first_error:
                    error_text = str(first_error)

                    # OBS WebSocket request-level hatalari bağlantı
                    # kopmasi değildir. Örneğin GetInputMute 604:
                    # "The specified input does not support audio."
                    #
                    # Böyle durumlarda ReqClient'i kapatıp yeniden
                    # bağlanmak yerine hatayı çağıran endpoint'e bırak.
                    if (
                        "returned code 604" in error_text
                        or "does not support audio" in error_text
                    ):
                        raise

                    # Diğer hatalar gerçek bağlantı problemi olabilir.
                    # Bir kez reconnect edip isteği tekrar deniyoruz.
                    print(
                        f"OBS connection/request error ({name}), "
                        f"reconnecting: {first_error}",
                        flush=True
                    )

                    obs_reset_client()

                    try:
                        client = _obs_connect()
                        method = getattr(client, name)
                        return method(*args, **kwargs)

                    except Exception:
                        obs_reset_client()
                        raise

        return call


_obs_proxy = OBSClientProxy()


def obs_client():
    return _obs_proxy


@app.get("/api/obs/status")
def obs_status():
    try:
        cl = obs_client()

        version = cl.get_version()
        stream = cl.get_stream_status()
        scene = cl.get_current_program_scene()

        return {
            "connected": True,
            "obs_version": getattr(version, "obs_version", ""),
            "websocket_version": getattr(
                version,
                "obs_web_socket_version",
                ""
            ),
            "streaming": getattr(stream, "output_active", False),
            "stream_timecode": getattr(
                stream,
                "output_timecode",
                "00:00:00"
            ),
            "current_scene": getattr(
                scene,
                "current_program_scene_name",
                ""
            )
        }

    except Exception as e:
        return {
            "connected": False,
            "error": str(e)
        }


@app.get("/api/obs/scenes")
def obs_scenes():
    try:
        cl = obs_client()
        response = cl.get_scene_list()

        return {
            "current_scene":
                response.current_program_scene_name,
            "scenes": [
                scene["sceneName"]
                for scene in response.scenes
            ]
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"OBS sahneleri alınamadı: {e}"
        )


class OBSSceneRequest(BaseModel):
    scene: str


@app.post("/api/obs/scene")
def obs_change_scene(req: OBSSceneRequest):
    try:
        cl = obs_client()

        cl.set_current_program_scene(req.scene)

        return {
            "ok": True,
            "scene": req.scene
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Sahne değiştirilemedi: {e}"
        )


@app.post("/api/obs/stream/start")
def obs_stream_start():
    try:
        cl = obs_client()
        cl.start_stream()

        return {"ok": True}

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Yayın başlatılamadı: {e}"
        )


@app.post("/api/obs/stream/stop")
def obs_stream_stop():
    try:
        cl = obs_client()
        cl.stop_stream()

        return {"ok": True}

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Yayın durdurulamadı: {e}"
        )


@app.get("/api/obs/audio")
def obs_audio():
    try:
        cl = obs_client()

        inputs = cl.get_input_list()

        result = []

        # Windows/OBS gerçek ses capture kaynakları.
        # Audio desteklemeyen video/image vb. input'lara GetInputMute
        # gönderilirse OBS 604 döndürüyor.
        audio_input_kinds = {
            "wasapi_input_capture",
            "wasapi_output_capture",
        }

        for item in inputs.inputs:

            name = item["inputName"]
            kind = item.get("inputKind", "")

            if kind not in audio_input_kinds:
                continue

            try:
                mute = cl.get_input_mute(name)

                result.append({
                    "name": name,
                    "kind": kind,
                    "muted": mute.input_muted
                })

            except Exception:
                # Tek bir ses kaynağındaki hata tüm endpoint'i bozmasın.
                continue

        return result

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"OBS ses kaynakları alınamadı: {e}"
        )


class OBSMuteRequest(BaseModel):
    input: str
    muted: bool


@app.post("/api/obs/audio/mute")
def obs_audio_mute(req: OBSMuteRequest):
    try:
        cl = obs_client()

        cl.set_input_mute(
            req.input,
            req.muted
        )

        return {
            "ok": True,
            "input": req.input,
            "muted": req.muted
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Ses durumu değiştirilemedi: {e}"
        )


@app.get("/api/obs/screenshot")
def obs_screenshot():
    try:
        cl = obs_client()

        scene = cl.get_current_program_scene()
        scene_name = scene.current_program_scene_name

        screenshot = cl.get_source_screenshot(
            scene_name,
            "jpg",
            640,
            360,
            70
        )

        image_data = screenshot.image_data

        # OBS:
        # data:image/jpeg;base64,xxxxx
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        return {
            "scene": scene_name,
            "image":
                "data:image/jpeg;base64,"
                + image_data
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"OBS görüntüsü alınamadı: {e}"
        )
# ---------------------------------------------------------
# UBUNTU HOST MONITORING
# ---------------------------------------------------------

import shutil
import time

_host_net_previous = None
_host_net_previous_time = None


def read_host_cpu():
    with open("/host/proc/stat", "r") as f:
        values = f.readline().split()[1:]

    values = [int(x) for x in values]

    idle = values[3] + values[4]
    total = sum(values)

    time.sleep(0.15)

    with open("/host/proc/stat", "r") as f:
        values2 = [int(x) for x in f.readline().split()[1:]]

    idle2 = values2[3] + values2[4]
    total2 = sum(values2)

    total_delta = total2 - total
    idle_delta = idle2 - idle

    if total_delta <= 0:
        return 0

    return round(
        100 * (1 - idle_delta / total_delta),
        1
    )


def read_host_memory():

    info = {}

    with open("/host/proc/meminfo", "r") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.strip().split()[0])

    total = info["MemTotal"] * 1024
    available = info["MemAvailable"] * 1024
    used = total - available

    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": round((used / total) * 100, 1)
    }


def read_host_network():

    global _host_net_previous
    global _host_net_previous_time

    rx = 0
    tx = 0

    interfaces = []

    with open("/host/proc/net/dev", "r") as f:

        for line in f.readlines()[2:]:

            interface, data = line.split(":", 1)
            interface = interface.strip()

            if interface == "lo":
                continue

            values = data.split()

            interface_rx = int(values[0])
            interface_tx = int(values[8])

            rx += interface_rx
            tx += interface_tx

            interfaces.append({
                "name": interface,
                "rx": interface_rx,
                "tx": interface_tx
            })

    now = time.time()

    down_mbps = 0
    up_mbps = 0

    if _host_net_previous is not None:

        elapsed = now - _host_net_previous_time

        if elapsed > 0:

            down_mbps = (
                (rx - _host_net_previous["rx"])
                * 8 / elapsed / 1_000_000
            )

            up_mbps = (
                (tx - _host_net_previous["tx"])
                * 8 / elapsed / 1_000_000
            )

    _host_net_previous = {
        "rx": rx,
        "tx": tx
    }

    _host_net_previous_time = now

    return {
        "down_mbps": round(max(0, down_mbps), 2),
        "up_mbps": round(max(0, up_mbps), 2),
        "rx_total": rx,
        "tx_total": tx,
        "interfaces": interfaces
    }


@app.get("/api/host/stats")
def host_stats():

    cpu = read_host_cpu()
    memory = read_host_memory()

    disk = shutil.disk_usage("/host/root")

    network = read_host_network()

    cpu_count = os.cpu_count()

    return {

        "hostname": "terminator",

        "cpu": {
            "percent": cpu,
            "cores": cpu_count
        },

        "memory": {
            "percent": memory["percent"],
            "total_gb": round(memory["total"] / 1073741824, 2),
            "used_gb": round(memory["used"] / 1073741824, 2),
            "free_gb": round(memory["available"] / 1073741824, 2)
        },

        "disk": {
            "percent": round(
                disk.used / disk.total * 100,
                1
            ),

            "total_gb": round(
                disk.total / 1073741824,
                2
            ),

            "used_gb": round(
                disk.used / 1073741824,
                2
            ),

            "free_gb": round(
                disk.free / 1073741824,
                2
            )
        },

        "network": network
    }
# ---------------------------------------------------------
# OBS LIVE AUDIO METERS
# ---------------------------------------------------------

obs_meter_clients = set()
obs_meter_loop = None
obs_meter_event_client = None
obs_meter_lock = threading.Lock()
obs_meter_watchdog_task = None

# OBS'den en son gerçek InputVolumeMeters event'inin geldiği zaman.
# monotonic kullanıyoruz; sistem saatindeki değişikliklerden etkilenmez.
obs_meter_last_event = 0.0


def mul_to_db(value):
    try:
        value = float(value)

        if value <= 0:
            return -60.0

        return max(-60.0, min(0.0, 20.0 * math.log10(value)))

    except Exception:
        return -60.0


def on_input_volume_meters(data):
    global obs_meter_loop, obs_meter_last_event

    # Callback'e ulaştıysak OBS gerçekten meter event'i gönderiyor.
    obs_meter_last_event = time.monotonic()

    try:
        # obsws-python 1.8.0 callback bize class/type benzeri
        # bir nesne veriyor; inputs doğrudan attribute olarak mevcut.
        inputs = getattr(data, "inputs", [])

        result = []

        for item in inputs:

            name = item.get("inputName", "")
            levels = item.get("inputLevelsMul", [])

            channels = []

            for channel in levels:

                if not channel:
                    channels.append(-60.0)
                    continue

                # OBS her kanal için birkaç ölçüm gönderiyor.
                # Canlı meter için en yüksek değeri kullan.
                level = max(float(x or 0) for x in channel)

                channels.append(round(mul_to_db(level), 1))

            result.append({
                "name": name,
                "uuid": item.get("inputUuid", ""),
                "channels": channels
            })

        if not obs_meter_loop:
            return

        payload = {
            "type": "audio_meter",
            "inputs": result
        }

        asyncio.run_coroutine_threadsafe(
            broadcast_obs_meter(payload),
            obs_meter_loop
        )

    except Exception as e:
        print("OBS meter event error:", e)


async def broadcast_obs_meter(payload):

    dead = []

    for websocket in list(obs_meter_clients):

        try:
            await websocket.send_json(payload)

        except Exception:
            dead.append(websocket)

    for websocket in dead:
        obs_meter_clients.discard(websocket)


def stop_obs_meter_client():
    global obs_meter_event_client

    with obs_meter_lock:
        client = obs_meter_event_client
        obs_meter_event_client = None

        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


def start_obs_meter_client():
    global obs_meter_event_client, obs_meter_last_event

    with obs_meter_lock:
        if obs_meter_event_client is not None:
            return True

        try:
            settings = settings_load()
            obs_cfg = settings.get("obs", {})

            host = str(obs_cfg.get("host") or OBS_HOST).strip()

            try:
                port = int(obs_cfg.get("port") or OBS_PORT)
            except (TypeError, ValueError):
                port = OBS_PORT

            password = obs_cfg.get("password") or OBS_PASSWORD or ""

            client = obs.EventClient(
                host=host,
                port=port,
                password=password,
                subs=Subs.INPUTVOLUMEMETERS
            )

            client.callback.register(on_input_volume_meters)

            obs_meter_event_client = client

            # Bağlantı yeni kuruldu. İlk event'in gelmesi için watchdog'a
            # kısa bir süre tanıyoruz.
            obs_meter_last_event = time.monotonic()

            print(
                "OBS audio meter EventClient connected",
                flush=True
            )

            return True

        except Exception as e:
            obs_meter_event_client = None
            obs_meter_last_event = 0.0

            print(
                "OBS audio meter connection error:",
                repr(e),
                flush=True
            )

            return False


async def obs_meter_watchdog():
    global obs_meter_event_client, obs_meter_last_event

    while True:
        await asyncio.sleep(3)

        # Dashboard'da meter dinleyen kimse yoksa OBS'yi gereksiz yere
        # reconnect etmiyoruz.
        if not obs_meter_clients:
            continue

        with obs_meter_lock:
            client_exists = obs_meter_event_client is not None

        now = time.monotonic()

        # EventClient hiç yoksa bağlantıyı kurmayı dene.
        if not client_exists:
            print(
                "OBS audio meter client missing - reconnecting",
                flush=True
            )

            await asyncio.to_thread(start_obs_meter_client)
            continue

        # Client yeni oluşturulduysa ilk event için zaman tanı.
        if obs_meter_last_event <= 0:
            continue

        event_age = now - obs_meter_last_event

        # InputVolumeMeters normalde sürekli gelir.
        # 10 saniyedir hiç event gelmediyse bağlantıyı ölü kabul ediyoruz.
        if event_age <= 10:
            continue

        print(
            f"OBS audio meter stale ({event_age:.1f}s) - reconnecting",
            flush=True
        )

        await asyncio.to_thread(stop_obs_meter_client)

        # OBS yeni açılıyorsa çok agresif reconnect yapmayalım.
        await asyncio.sleep(1)

        await asyncio.to_thread(start_obs_meter_client)


@app.websocket("/ws/obs/audio")
async def websocket_obs_audio(websocket: WebSocket):

    global obs_meter_loop, obs_meter_watchdog_task

    await websocket.accept()

    obs_meter_loop = asyncio.get_running_loop()

    obs_meter_clients.add(websocket)

    await asyncio.to_thread(start_obs_meter_client)

    if obs_meter_watchdog_task is None or obs_meter_watchdog_task.done():
        obs_meter_watchdog_task = asyncio.create_task(
            obs_meter_watchdog()
        )

    try:

        while True:
            # Browser bağlantısının açık olduğunu takip ediyoruz.
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    except Exception:
        pass

    finally:
        obs_meter_clients.discard(websocket)

# ---------------------------------------------------------
# YOUTUBE OAUTH / LIVE CHAT
# ---------------------------------------------------------

def youtube_oauth_settings():
    """
    YouTube OAuth ayarlarını dashboard settings.json içinden alır.
    settings.json içinde değer yoksa eski .env değerleri fallback
    olarak kullanılmaya devam eder.
    """
    settings = settings_load()
    youtube_cfg = settings.get("youtube") or {}

    client_id = (
        str(youtube_cfg.get("client_id") or "").strip()
        or YOUTUBE_CLIENT_ID
    )

    client_secret = (
        str(youtube_cfg.get("client_secret") or "").strip()
        or YOUTUBE_CLIENT_SECRET
    )

    redirect_uri = (
        str(youtube_cfg.get("redirect_uri") or "").strip()
        or YOUTUBE_REDIRECT_URI
    )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri
    }


def youtube_client_config():
    cfg = youtube_oauth_settings()

    return {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg["redirect_uri"]],
        }
    }


def youtube_load_credentials():
    if not os.path.exists(YOUTUBE_TOKEN_FILE):
        return None

    try:
        creds = Credentials.from_authorized_user_file(
            YOUTUBE_TOKEN_FILE,
            YOUTUBE_SCOPES
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())

            with open(YOUTUBE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        if not creds.valid:
            return None

        return creds

    except Exception as e:
        print("YouTube credentials error:", e, flush=True)
        return None


def youtube_service():
    creds = youtube_load_credentials()

    if not creds:
        return None

    return build(
        "youtube",
        "v3",
        credentials=creds,
        cache_discovery=False
    )


# =========================================================
# YOUTUBE LIVE DISCOVERY CACHE
# =========================================================

_youtube_live_lock = threading.RLock()
_youtube_live_cache = None
_youtube_live_cache_until = 0.0
_youtube_quota_backoff_until = 0.0

# Aktif yayın bulunduğunda keşif sonucunu bu süre boyunca kullan.
YOUTUBE_LIVE_CACHE_SECONDS = 60

# Yayın yoksa Google API'yi gereksiz yere sürekli sorgulama.
YOUTUBE_NO_LIVE_CACHE_SECONDS = 30

# Günlük kota dolduğunda sürekli 403 üretmemek için uzun backoff.
YOUTUBE_QUOTA_BACKOFF_SECONDS = 3600

# İzleyici sayısı için ayrı cache.
# Chat polling'inden bağımsız çalışır.
_youtube_viewer_lock = threading.RLock()
_youtube_viewer_cache = None
_youtube_viewer_cache_until = 0.0

YOUTUBE_VIEWER_CACHE_SECONDS = 60

# =========================================================
# YOUTUBE CHAT SHARED POLLING STATE
# =========================================================
#
# Google YouTube API'ye polling browser tarafında değil,
# backend tarafında tek zincir halinde yapılır.
#
# Böylece birden fazla dashboard istemcisi açık olsa bile
# her istemci ayrı liveChatMessages.list() çağrısı üretmez.
#
_youtube_chat_lock = threading.RLock()
_youtube_chat_live_chat_id = None
_youtube_chat_next_page_token = None
_youtube_chat_messages = []
_youtube_chat_next_poll_at = 0.0
_youtube_chat_polling_interval_ms = 5000

YOUTUBE_CHAT_MAX_MESSAGES = 200


def youtube_is_quota_error(error):
    text = str(error).lower()

    return (
        "quotaexceeded" in text
        or "youtube.quota" in text
        or "exceeded your" in text and "quota" in text
    )


def youtube_discover_live(youtube, force=False):
    global _youtube_live_cache
    global _youtube_live_cache_until
    global _youtube_quota_backoff_until

    now = time.time()

    with _youtube_live_lock:

        if now < _youtube_quota_backoff_until:
            retry_after = max(
                1,
                int(_youtube_quota_backoff_until - now)
            )

            raise HTTPException(
                status_code=429,
                detail={
                    "code": "youtube_quota_exceeded",
                    "message": "YouTube API kotası doldu",
                    "retry_after": retry_after
                }
            )

        if (
            not force
            and _youtube_live_cache is not None
            and now < _youtube_live_cache_until
        ):
            return _youtube_live_cache

        try:
            response = youtube.liveBroadcasts().list(
                part="id,snippet,status",
                broadcastType="all",
                mine=True,
                maxResults=5
            ).execute()

        except Exception as e:

            if youtube_is_quota_error(e):
                _youtube_quota_backoff_until = (
                    time.time()
                    + YOUTUBE_QUOTA_BACKOFF_SECONDS
                )

                print(
                    "YouTube quota exceeded; "
                    f"backoff {YOUTUBE_QUOTA_BACKOFF_SECONDS}s",
                    flush=True
                )

                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "youtube_quota_exceeded",
                        "message": "YouTube API kotası doldu",
                        "retry_after": YOUTUBE_QUOTA_BACKOFF_SECONDS
                    }
                )

            raise

        items = [
            item
            for item in response.get("items", [])
            if (
                item.get("status", {})
                .get("lifeCycleStatus") == "live"
            )
        ]

        if not items:
            result = {
                "live": False,
                "message": "Aktif YouTube yayını bulunamadı"
            }

            _youtube_live_cache = result
            _youtube_live_cache_until = (
                time.time()
                + YOUTUBE_NO_LIVE_CACHE_SECONDS
            )

            return result

        broadcast = items[0]
        snippet = broadcast.get("snippet", {})

        result = {
            "live": True,
            "broadcast_id": broadcast.get("id"),
            "title": snippet.get("title", ""),
            "live_chat_id": snippet.get("liveChatId"),
            "published_at": snippet.get("publishedAt")
        }

        _youtube_live_cache = result
        _youtube_live_cache_until = (
            time.time()
            + YOUTUBE_LIVE_CACHE_SECONDS
        )

        return result


@app.get("/api/youtube/status")
def youtube_status():
    cfg = youtube_oauth_settings()
    creds = youtube_load_credentials()

    return {
        "configured": bool(
            cfg["client_id"]
            and cfg["client_secret"]
            and cfg["redirect_uri"]
        ),
        "authenticated": bool(creds)
    }


@app.get("/api/youtube/auth")
def youtube_auth():
    cfg = youtube_oauth_settings()

    if (
        not cfg["client_id"]
        or not cfg["client_secret"]
        or not cfg["redirect_uri"]
    ):
        raise HTTPException(
            status_code=500,
            detail="YouTube OAuth yapılandırılmamış"
        )

    flow = Flow.from_client_config(
        youtube_client_config(),
        scopes=YOUTUBE_SCOPES,
        autogenerate_code_verifier=False
    )

    flow.redirect_uri = cfg["redirect_uri"]

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    return RedirectResponse(authorization_url)


@app.get("/api/youtube/callback")
def youtube_callback(code: str = Query(...)):
    try:
        cfg = youtube_oauth_settings()

        if (
            not cfg["client_id"]
            or not cfg["client_secret"]
            or not cfg["redirect_uri"]
        ):
            raise HTTPException(
                status_code=500,
                detail="YouTube OAuth yapılandırılmamış"
            )

        flow = Flow.from_client_config(
            youtube_client_config(),
            scopes=YOUTUBE_SCOPES,
            autogenerate_code_verifier=False
        )

        flow.redirect_uri = cfg["redirect_uri"]
        flow.fetch_token(code=code)

        creds = flow.credentials

        os.makedirs(
            os.path.dirname(YOUTUBE_TOKEN_FILE),
            exist_ok=True
        )

        with open(
            YOUTUBE_TOKEN_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(creds.to_json())

        return HTMLResponse("""
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<title>YouTube Bağlandı</title>

<style>
body{
    background:#06111b;
    color:#d8e5f2;
    font-family:Arial,sans-serif;
    display:flex;
    align-items:center;
    justify-content:center;
    min-height:100vh;
    margin:0;
}

.box{
    background:#0b1c29;
    border:1px solid #173149;
    border-radius:18px;
    padding:30px;
    text-align:center;
}

h2{
    color:#22df82;
}
</style>
</head>

<body>

<div class="box">
    <h2>✓ YouTube bağlandı</h2>
    <p>Yetkilendirme tamamlandı.</p>
    <p>Bu pencere otomatik kapanacak.</p>
</div>

<script>
try{
    if(window.opener){
        window.opener.postMessage(
            {type:"youtube-oauth-success"},
            window.location.origin
        );
    }
}catch(e){}

setTimeout(function(){
    window.close();
},1000);
</script>

</body>
</html>
        """)

    except HTTPException:
        raise

    except Exception as e:
        print(
            "YouTube OAuth callback error:",
            repr(e),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="YouTube OAuth işlemi başarısız"
        )


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    global _youtube_live_cache
    global _youtube_live_cache_until
    global _youtube_quota_backoff_until

    try:
        if os.path.exists(YOUTUBE_TOKEN_FILE):
            os.remove(YOUTUBE_TOKEN_FILE)

    except Exception as e:
        print(
            "YouTube disconnect error:",
            repr(e),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="YouTube bağlantısı kaldırılamadı"
        )

    _youtube_live_cache = None
    _youtube_live_cache_until = 0.0
    _youtube_quota_backoff_until = 0.0

    return {
        "ok": True,
        "authenticated": False
    }


@app.get("/api/youtube/viewers")
def youtube_viewers():
    global _youtube_viewer_cache
    global _youtube_viewer_cache_until
    global _youtube_quota_backoff_until

    try:
        youtube = youtube_service()

        if youtube is None:
            raise HTTPException(
                status_code=401,
                detail="YouTube hesabı bağlı değil"
            )

        # Aktif yayını mevcut ortak cache üzerinden bul.
        live_info = youtube_discover_live(youtube)

        if not live_info.get("live"):
            return {
                "live": False,
                "viewers": 0
            }

        broadcast_id = live_info.get("broadcast_id")

        if not broadcast_id:
            return {
                "live": True,
                "viewers": None
            }

        now = time.time()

        with _youtube_viewer_lock:

            if (
                _youtube_viewer_cache is not None
                and now < _youtube_viewer_cache_until
                and _youtube_viewer_cache.get(
                    "broadcast_id"
                ) == broadcast_id
            ):
                return _youtube_viewer_cache

            try:
                response = youtube.videos().list(
                    part="liveStreamingDetails",
                    id=broadcast_id
                ).execute()

            except Exception as e:

                if youtube_is_quota_error(e):
                    _youtube_quota_backoff_until = (
                        time.time()
                        + YOUTUBE_QUOTA_BACKOFF_SECONDS
                    )

                    raise HTTPException(
                        status_code=429,
                        detail={
                            "code":
                                "youtube_quota_exceeded",
                            "message":
                                "YouTube API kotası doldu",
                            "retry_after":
                                YOUTUBE_QUOTA_BACKOFF_SECONDS
                        }
                    )

                raise

            items=response.get("items", [])

            viewers=None

            if items:
                details=items[0].get(
                    "liveStreamingDetails",
                    {}
                )

                raw_viewers=details.get(
                    "concurrentViewers"
                )

                if raw_viewers is not None:
                    try:
                        viewers=int(raw_viewers)
                    except (TypeError,ValueError):
                        viewers=None

            result={
                "live": True,
                "broadcast_id": broadcast_id,
                "viewers": viewers
            }

            _youtube_viewer_cache=result
            _youtube_viewer_cache_until=(
                time.time()
                + YOUTUBE_VIEWER_CACHE_SECONDS
            )

            return result

    except HTTPException:
        raise

    except Exception as e:
        print(
            "YouTube viewers error:",
            repr(e),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="YouTube izleyici sayısı alınamadı"
        )


@app.get("/api/youtube/live")
def youtube_live():
    try:
        youtube = youtube_service()

        if youtube is None:
            raise HTTPException(
                status_code=401,
                detail="YouTube hesabı bağlı değil"
            )

        return youtube_discover_live(youtube)

    except HTTPException:
        raise

    except Exception as e:
        print("YouTube live error:", e, flush=True)
        raise HTTPException(
            status_code=500,
            detail=f"YouTube canlı yayın hatası: {e}"
        )


@app.get("/api/youtube/chat")
def youtube_chat():
    global _youtube_chat_live_chat_id
    global _youtube_chat_next_page_token
    global _youtube_chat_messages
    global _youtube_chat_next_poll_at
    global _youtube_chat_polling_interval_ms
    global _youtube_quota_backoff_until

    try:
        youtube = youtube_service()

        if youtube is None:
            raise HTTPException(
                status_code=401,
                detail="YouTube hesabı bağlı değil"
            )

        now = time.time()

        # -------------------------------------------------
        # Ortak YouTube kota backoff kontrolü.
        # -------------------------------------------------
        if now < _youtube_quota_backoff_until:
            retry_after = max(
                1,
                int(_youtube_quota_backoff_until - now)
            )

            raise HTTPException(
                status_code=429,
                detail={
                    "code": "youtube_quota_exceeded",
                    "message": "YouTube API kotası doldu",
                    "retry_after": retry_after
                }
            )

        # -------------------------------------------------
        # Aktif yayını ortak discovery cache üzerinden bul.
        # -------------------------------------------------
        live_info = youtube_discover_live(youtube)

        if not live_info.get("live"):
            with _youtube_chat_lock:
                _youtube_chat_live_chat_id = None
                _youtube_chat_next_page_token = None
                _youtube_chat_messages = []
                _youtube_chat_next_poll_at = 0.0
                _youtube_chat_polling_interval_ms = 5000

            return {
                "live": False,
                "messages": [],
                "message": live_info.get(
                    "message",
                    "Aktif YouTube yayını bulunamadı"
                )
            }

        live_chat_id = live_info.get("live_chat_id")

        if not live_chat_id:
            return {
                "live": True,
                "chat_available": False,
                "messages": [],
                "message": "Canlı sohbet kullanılamıyor"
            }

        with _youtube_chat_lock:

            now = time.time()

            # -------------------------------------------------
            # Yeni yayın/chat başladıysa eski state'i temizle.
            # -------------------------------------------------
            if _youtube_chat_live_chat_id != live_chat_id:
                _youtube_chat_live_chat_id = live_chat_id
                _youtube_chat_next_page_token = None
                _youtube_chat_messages = []
                _youtube_chat_next_poll_at = 0.0
                _youtube_chat_polling_interval_ms = 5000

            # -------------------------------------------------
            # Google'ın önerdiği polling süresi henüz dolmadıysa
            # Google'a gitmeden mevcut cache'i döndür.
            # -------------------------------------------------
            if now < _youtube_chat_next_poll_at:
                return {
                    "live": True,
                    "chat_available": True,
                    "live_chat_id":
                        _youtube_chat_live_chat_id,
                    "polling_interval_ms":
                        _youtube_chat_polling_interval_ms,
                    "messages":
                        list(_youtube_chat_messages)
                }

            kwargs = {
                "liveChatId": live_chat_id,
                "part": "id,snippet,authorDetails",
                "maxResults": 200
            }

            if _youtube_chat_next_page_token:
                kwargs["pageToken"] = (
                    _youtube_chat_next_page_token
                )

            try:
                response = (
                    youtube
                    .liveChatMessages()
                    .list(**kwargs)
                    .execute()
                )

            except Exception as e:

                if youtube_is_quota_error(e):
                    _youtube_quota_backoff_until = (
                        time.time()
                        + YOUTUBE_QUOTA_BACKOFF_SECONDS
                    )

                    print(
                        "YouTube chat quota exceeded; "
                        f"backoff "
                        f"{YOUTUBE_QUOTA_BACKOFF_SECONDS}s",
                        flush=True
                    )

                    raise HTTPException(
                        status_code=429,
                        detail={
                            "code":
                                "youtube_quota_exceeded",
                            "message":
                                "YouTube API kotası doldu",
                            "retry_after":
                                YOUTUBE_QUOTA_BACKOFF_SECONDS
                        }
                    )

                raise

            messages = []

            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                author = item.get(
                    "authorDetails",
                    {}
                )

                messages.append({
                    "id": item.get("id"),
                    "author": author.get(
                        "displayName",
                        ""
                    ),
                    "author_channel_id": author.get(
                        "channelId",
                        ""
                    ),
                    "avatar": author.get(
                        "profileImageUrl",
                        ""
                    ),
                    "message": snippet.get(
                        "displayMessage",
                        ""
                    ),
                    "published_at": snippet.get(
                        "publishedAt"
                    ),
                    "is_owner": author.get(
                        "isChatOwner",
                        False
                    ),
                    "is_moderator": author.get(
                        "isChatModerator",
                        False
                    ),
                    "is_member": author.get(
                        "isChatSponsor",
                        False
                    )
                })

            # -------------------------------------------------
            # ID bazlı duplicate engelleme.
            # -------------------------------------------------
            existing = {
                item.get("id")
                for item in _youtube_chat_messages
                if item.get("id")
            }

            for message in messages:
                message_id = message.get("id")

                if (
                    message_id
                    and message_id not in existing
                ):
                    _youtube_chat_messages.append(
                        message
                    )
                    existing.add(message_id)

            if (
                len(_youtube_chat_messages)
                > YOUTUBE_CHAT_MAX_MESSAGES
            ):
                _youtube_chat_messages = (
                    _youtube_chat_messages[
                        -YOUTUBE_CHAT_MAX_MESSAGES:
                    ]
                )

            next_token = response.get(
                "nextPageToken"
            )

            if next_token:
                _youtube_chat_next_page_token = (
                    next_token
                )

            try:
                interval_ms = int(
                    response.get(
                        "pollingIntervalMillis",
                        5000
                    )
                )
            except (TypeError, ValueError):
                interval_ms = 5000

            # YouTube daha kısa bir değer döndürse bile
            # minimum 2 saniye koruma uygula.
            interval_ms = max(
                2000,
                interval_ms
            )

            _youtube_chat_polling_interval_ms = (
                interval_ms
            )

            _youtube_chat_next_poll_at = (
                time.time()
                + (interval_ms / 1000.0)
            )

            return {
                "live": True,
                "chat_available": True,
                "live_chat_id":
                    _youtube_chat_live_chat_id,
                "polling_interval_ms":
                    _youtube_chat_polling_interval_ms,
                "messages":
                    list(_youtube_chat_messages)
            }

    except HTTPException:
        raise

    except Exception as e:
        print(
            "YouTube chat error:",
            repr(e),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="YouTube sohbet hatası"
        )


# =========================================================
# DASHBOARD SETTINGS
# =========================================================

SETTINGS_FILE = "/app/data/settings.json"
_settings_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "youtube": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": ""
    },
    "kick": {
        "channel": "golg3",
        "channel_id": 27125816,
        "chatroom_id": 26837501
    },
    "weather": {
        "location": "",
        "latitude": None,
        "longitude": None
    },
    "obs": {
        "host": "10.29.250.13",
        "port": 4455,
        "password": ""
    },
    "docker": {
        "connection_type": "local",
        "socket": "/var/run/docker.sock",
        "host": ""
    },
    "host_monitor": {
        "name": "terminator",
        "proc_path": "/host/proc",
        "root_path": "/host/root",
        "network_interface": "auto"
    },
    "windows_monitor": {
        "name": "",
        "expected_hostname": "",
        "agent_token": "",
        "timeout": 15
    }
}


def settings_deep_copy(value):
    return json.loads(json.dumps(value))


def settings_deep_merge(base, override):
    result = settings_deep_copy(base)

    if not isinstance(override, dict):
        return result

    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = settings_deep_merge(
                result[key],
                value
            )
        else:
            result[key] = value

    return result


def settings_load():
    with _settings_lock:
        if not os.path.exists(SETTINGS_FILE):
            return settings_deep_copy(DEFAULT_SETTINGS)

        try:
            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8"
            ) as f:
                stored = json.load(f)

            return settings_deep_merge(
                DEFAULT_SETTINGS,
                stored
            )

        except Exception as e:
            print(
                "Settings load error:",
                e,
                flush=True
            )
            return settings_deep_copy(DEFAULT_SETTINGS)


def settings_save(settings):
    with _settings_lock:
        directory = os.path.dirname(SETTINGS_FILE)
        os.makedirs(directory, exist_ok=True)

        temp_file = SETTINGS_FILE + ".tmp"

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                settings,
                f,
                ensure_ascii=False,
                indent=2
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(
            temp_file,
            SETTINGS_FILE
        )


def settings_public(settings):
    public = settings_deep_copy(settings)

    youtube = public.setdefault("youtube", {})
    youtube_secret = youtube.pop(
        "client_secret",
        ""
    )
    youtube["client_secret_set"] = bool(
        youtube_secret or YOUTUBE_CLIENT_SECRET
    )

    obs_cfg = public.setdefault("obs", {})
    obs_password = obs_cfg.pop(
        "password",
        ""
    )
    obs_cfg["password_set"] = bool(
        obs_password or os.getenv("OBS_PASSWORD", "")
    )

    windows = public.setdefault(
        "windows_monitor",
        {}
    )
    agent_token = windows.pop(
        "agent_token",
        ""
    )
    windows["agent_token_set"] = bool(
        agent_token or TOKEN
    )

    return public


def settings_clean_string(value, max_length=1024):
    if value is None:
        return ""

    value = str(value).strip()

    if len(value) > max_length:
        raise HTTPException(
            status_code=400,
            detail="Ayar değeri çok uzun"
        )

    return value



def kick_resolve_channel(channel):
    """
    Kick kanal adı veya URL'sinden channel_id ve chatroom_id bulur.
    Örnek:
      naru
      https://kick.com/naru
    """
    import urllib.request
    import urllib.parse

    channel = settings_clean_string(channel, 255).strip()

    if not channel:
        raise HTTPException(
            status_code=400,
            detail="Kick kanal adı boş olamaz"
        )

    # URL verilmişse slug'ı çıkar.
    if "://" in channel:
        try:
            parsed = urllib.parse.urlparse(channel)
            channel = parsed.path.strip("/").split("/")[0]
        except Exception:
            pass

    channel = channel.strip().strip("/")

    if not channel:
        raise HTTPException(
            status_code=400,
            detail="Kick kanal adı geçersiz"
        )

    url = (
        "https://kick.com/api/v2/channels/"
        + urllib.parse.quote(channel)
    )

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=10
        ) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

    except Exception as e:
        print(
            "Kick channel lookup error:",
            repr(e),
            flush=True
        )
        raise HTTPException(
            status_code=502,
            detail="Kick kanal bilgisi alınamadı"
        )

    channel_id = data.get("id")

    chatroom = data.get("chatroom") or {}
    chatroom_id = chatroom.get("id")

    if not channel_id:
        raise HTTPException(
            status_code=404,
            detail="Kick Channel ID bulunamadı"
        )

    if not chatroom_id:
        raise HTTPException(
            status_code=404,
            detail="Kick Chatroom ID bulunamadı"
        )

    return {
        "channel": channel,
        "channel_id": int(channel_id),
        "chatroom_id": int(chatroom_id)
    }



def settings_apply_update(current, incoming):
    if not isinstance(incoming, dict):
        raise HTTPException(
            status_code=400,
            detail="Geçersiz ayar verisi"
        )

    allowed_sections = {
        "youtube",
        "kick",
        "weather",
        "obs",
        "docker",
        "host_monitor",
        "windows_monitor"
    }

    result = settings_deep_copy(current)

    # Kick için kullanıcı sadece kanal adı / URL girer.
    # Channel ID ve Chatroom ID otomatik bulunur.
    kick_incoming = incoming.get("kick")

    if isinstance(kick_incoming, dict) and "channel" in kick_incoming:
        resolved_kick = kick_resolve_channel(
            kick_incoming.get("channel")
        )

        incoming = settings_deep_copy(incoming)
        incoming["kick"] = resolved_kick

    for section, values in incoming.items():
        if section not in allowed_sections:
            continue

        if not isinstance(values, dict):
            continue

        result.setdefault(section, {})

        for key, value in values.items():

            # Frontend'e dönen durum alanlarını kaydetme.
            if key.endswith("_set"):
                continue

            # Secret alanları boş gönderilirse
            # mevcut secret korunur.
            if (
                section == "youtube"
                and key == "client_secret"
            ):
                value = settings_clean_string(value)

                if value:
                    result[section][key] = value

                continue

            if (
                section == "obs"
                and key == "password"
            ):
                value = settings_clean_string(value)

                if value:
                    result[section][key] = value

                continue

            if (
                section == "windows_monitor"
                and key == "agent_token"
            ):
                value = settings_clean_string(value)

                if value:
                    result[section][key] = value

                continue

            result[section][key] = value

    # Basit tip/range kontrolleri

    try:
        obs_port = int(
            result["obs"].get("port", 4455)
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="OBS port geçersiz"
        )

    if not 1 <= obs_port <= 65535:
        raise HTTPException(
            status_code=400,
            detail="OBS port 1-65535 arasında olmalı"
        )

    result["obs"]["port"] = obs_port

    try:
        timeout = int(
            result["windows_monitor"].get(
                "timeout",
                15
            )
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Windows monitor timeout geçersiz"
        )

    if not 1 <= timeout <= 3600:
        raise HTTPException(
            status_code=400,
            detail="Windows monitor timeout 1-3600 saniye arasında olmalı"
        )

    result["windows_monitor"]["timeout"] = timeout

    return result



@app.get("/api/kick/live")
def kick_live():
    """
    Kick kanalının canlı durumunu ve izleyici
    sayısını döndürür.
    """
    import urllib.request
    import urllib.parse

    settings = settings_load()

    kick_settings = settings.get("kick") or {}

    channel = settings_clean_string(
        kick_settings.get("channel", ""),
        255
    ).strip()

    if not channel:
        return {
            "live": False,
            "viewers": 0,
            "configured": False
        }

    if "://" in channel:
        try:
            parsed=urllib.parse.urlparse(channel)
            channel=(
                parsed.path
                .strip("/")
                .split("/")[0]
            )
        except Exception:
            pass

    channel=channel.strip().strip("/")

    if not channel:
        return {
            "live": False,
            "viewers": 0,
            "configured": False
        }

    url=(
        "https://kick.com/api/v2/channels/"
        + urllib.parse.quote(channel)
    )

    request=urllib.request.Request(
        url,
        headers={
            "Accept":"application/json",
            "User-Agent":"Mozilla/5.0"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=10
        ) as response:
            data=json.loads(
                response.read().decode("utf-8")
            )

    except Exception as e:
        print(
            "Kick live lookup error:",
            repr(e),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Kick canlı yayın bilgisi alınamadı"
        )

    livestream=data.get("livestream")

    if not livestream:
        return {
            "live": False,
            "viewers": 0,
            "channel": channel
        }

    # Kick v2 cevabında temel alan viewer_count.
    # Farklı cevap sürümlerine karşı birkaç güvenli
    # fallback da bırakıyoruz.
    raw_viewers=livestream.get("viewer_count")

    if raw_viewers is None:
        raw_viewers=livestream.get("viewers")

    if raw_viewers is None:
        raw_viewers=livestream.get(
            "concurrent_viewers"
        )

    try:
        viewers=(
            int(raw_viewers)
            if raw_viewers is not None
            else None
        )
    except (TypeError,ValueError):
        viewers=None

    return {
        "live": True,
        "viewers": viewers,
        "channel": channel
    }

@app.get("/api/settings")
def get_dashboard_settings():
    settings = settings_load()

    return {
        "ok": True,
        "settings": settings_public(settings)
    }


@app.put("/api/settings")
def update_dashboard_settings(payload: dict):
    current = settings_load()

    updated = settings_apply_update(
        current,
        payload
    )

    settings_save(updated)

    return {
        "ok": True,
        "settings": settings_public(updated)
    }

# =========================================================
# WEATHER LOCATION SEARCH
# =========================================================

@app.get("/api/settings/weather/search")
def search_weather_location(q: str = Query(..., min_length=2)):
    query = q.strip()

    params = urlencode({
        "name": query,
        "count": 10,
        "language": "tr",
        "format": "json"
    })

    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + params
    )

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "HomeDashboard/1.0"
            }
        )

        with urlopen(req, timeout=10) as response:
            raw = json.loads(
                response.read().decode("utf-8")
            )

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Konum servisine ulaşılamadı: {e}"
        )

    results = []

    for item in raw.get("results", []):
        results.append({
            "name": item.get("name", ""),
            "admin1": item.get("admin1", ""),
            "admin2": item.get("admin2", ""),
            "country": item.get("country", ""),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude")
        })

    return {
        "query": query,
        "results": results
    }
