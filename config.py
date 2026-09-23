"""F.R.I.D.A.Y. configuration.

Everything tunable lives here or in a `.env` file (see `.env.example`).
Environment variables always win over defaults; a real environment variable wins over `.env`.
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ───────────────────────────── Theme ─────────────────────────────
BG = "#0a0b10"
PANEL = "#0e1119"
CYAN = "#00f3ff"
ORANGE = "#ffaa00"
RED = "#ff3b3b"
TEXT = "#cfefff"
DIM = "#4a5a6a"


# ───────────────────────── .env loading ──────────────────────────
def _load_dotenv(path: Path) -> None:
    """Tiny .env reader (no extra dependency). Existing environment variables are never overwritten."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        elif " #" in value:  # strip trailing inline comment
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key.strip(), value)


_load_dotenv(PROJECT_DIR / ".env")
_load_dotenv(PACKAGE_DIR / ".env")


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else raw.strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _json(name: str) -> dict:
    raw = _s(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _allowed_roots() -> tuple:
    raw = _s("FRIDAY_ALLOWED_ROOTS")
    roots = [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    return tuple(roots) or (Path.home().resolve(),)


def _protected_paths() -> tuple:
    """Locations the file tools must never modify, even inside an allowed root."""
    if IS_WINDOWS:
        base = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramData", r"C:\ProgramData")]
    elif IS_MAC:
        base = ["/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/private", "/Applications"]
    else:
        base = ["/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin", "/sys", "/usr", "/var"]
    return tuple(Path(p).resolve() for p in base if p)


@dataclass
class Settings:
    # ── Brain ──────────────────────────────────────────────────
    api_key: str = _s("DIFFUSIONGEMMA_API_KEY")
    api_base_url: str = _s("FRIDAY_API_BASE_URL", "https://integrate.api.nvidia.com/v1")
    model: str = _s("FRIDAY_MODEL", "google/diffusiongemma-26b-a4b-it")
    extra_body: dict = field(default_factory=lambda: _json("FRIDAY_EXTRA_BODY"))
    temperature: float = _f("FRIDAY_TEMPERATURE", 0.6)
    max_tokens: int = _i("FRIDAY_MAX_TOKENS", 1024)
    request_timeout: float = _f("FRIDAY_REQUEST_TIMEOUT", 90.0)
    max_tool_rounds: int = _i("FRIDAY_MAX_TOOL_ROUNDS", 6)
    history_turns: int = _i("FRIDAY_HISTORY_TURNS", 14)
    model_supports_tools: bool = _b("FRIDAY_MODEL_SUPPORTS_TOOLS", True)
    model_supports_vision: bool = _b("FRIDAY_MODEL_SUPPORTS_VISION", True)
    user_title: str = _s("FRIDAY_USER_TITLE", "Boss")

    # ── Storage ────────────────────────────────────────────────
    data_dir: Path = Path(_s("FRIDAY_DATA_DIR") or (PROJECT_DIR / "data")).expanduser()

    # ── Wake word ──────────────────────────────────────────────
    wake_words: tuple = tuple(w.strip().lower() for w in _s("FRIDAY_WAKE_WORDS", "hey friday,friday").split(",") if w.strip())
    wake_backend: str = _s("FRIDAY_WAKE_BACKEND", "auto").lower()  # auto|porcupine|vosk|speech_recognition
    porcupine_access_key: str = _s("PICOVOICE_ACCESS_KEY")
    porcupine_keyword_path: str = _s("PORCUPINE_KEYWORD_PATH")
    porcupine_sensitivity: float = _f("PORCUPINE_SENSITIVITY", 0.6)
    vosk_model_path: str = _s("VOSK_MODEL_PATH")
    wake_cooldown_s: float = _f("FRIDAY_WAKE_COOLDOWN_S", 2.0)

    # ── Speech-to-text ─────────────────────────────────────────
    stt_engine: str = _s("FRIDAY_STT_ENGINE", "google").lower()  # google|vosk
    stt_language: str = _s("FRIDAY_STT_LANGUAGE", "en-GB")
    mic_index: Optional[int] = _i("FRIDAY_MIC_INDEX", -1) if _s("FRIDAY_MIC_INDEX") else None
    listen_timeout: float = _f("FRIDAY_LISTEN_TIMEOUT", 6.0)          # seconds to wait for speech to begin
    phrase_time_limit: float = _f("FRIDAY_PHRASE_LIMIT", 20.0)         # max length of one utterance
    session_max_silences: int = _i("FRIDAY_SESSION_MAX_SILENCES", 2)   # silent listens before returning to wake mode

    # ── Text-to-speech ─────────────────────────────────────────
    tts_engine: str = _s("FRIDAY_TTS_ENGINE", "pyttsx3").lower()  # pyttsx3|fish
    tts_voice_hint: str = _s("FRIDAY_TTS_VOICE")
    tts_rate: int = _i("FRIDAY_TTS_RATE", 185)
    tts_volume: float = _f("FRIDAY_TTS_VOLUME", 1.0)
    fish_api_key: str = _s("FISH_API_KEY")
    fish_model: str = _s("FISH_MODEL", "s2.1-pro-free")     # Fish Audio's free-tier model; set to a paid one if you upgrade
    fish_voice_id: str = _s("FISH_VOICE_ID")                # reference_id of a voice from fish.audio; blank = that model's default voice
    fish_sample_rate: int = _i("FISH_SAMPLE_RATE", 44100)
    speak_typed_replies: bool = _b("FRIDAY_SPEAK_TYPED", True)
    sound_effects: bool = _b("FRIDAY_SFX", True)

    # ── Vision / security ──────────────────────────────────────
    camera_index: int = _i("FRIDAY_CAMERA_INDEX", 0)
    security_autostart: bool = _b("FRIDAY_SECURITY_AUTOSTART", False)
    security_frame_interval_s: float = _f("FRIDAY_SECURITY_INTERVAL_S", 0.4)
    face_match_threshold: float = _f("FRIDAY_FACE_MATCH_THRESHOLD", 70.0)  # LBPH distance: LOWER = better match
    unknown_face_frames: int = _i("FRIDAY_UNKNOWN_FACE_FRAMES", 5)          # consecutive frames before alerting
    alert_cooldown_s: float = _f("FRIDAY_ALERT_COOLDOWN_S", 300.0)
    alert_on_motion: bool = _b("FRIDAY_ALERT_ON_MOTION", False)
    motion_min_area: int = _i("FRIDAY_MOTION_MIN_AREA", 5000)
    alert_if_no_faces_enrolled: bool = _b("FRIDAY_ALERT_IF_NO_FACES_ENROLLED", False)

    # ── WhatsApp (Twilio) ──────────────────────────────────────
    twilio_sid: str = _s("TWILIO_ACCOUNT_SID")
    twilio_token: str = _s("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_from: str = _s("TWILIO_WHATSAPP_FROM", "+14155238886")
    whatsapp_to: str = _s("WHATSAPP_TO")
    whatsapp_min_interval_s: float = _f("WHATSAPP_MIN_INTERVAL_S", 20.0)
    whatsapp_max_per_hour: int = _i("WHATSAPP_MAX_PER_HOUR", 12)

    # ── Fire TV ────────────────────────────────────────────────
    firetv_host: str = _s("FIRETV_HOST")
    firetv_port: int = _i("FIRETV_PORT", 5555)
    adb_host: str = _s("ADB_SERVER_HOST", "127.0.0.1")
    adb_port: int = _i("ADB_SERVER_PORT", 5037)
    adb_path: str = _s("ADB_PATH")
    firetv_extra_apps: dict = field(default_factory=lambda: _json("FIRETV_EXTRA_APPS"))  # {"friendly name": "package.id"}

    # ── Safety ─────────────────────────────────────────────────
    allowed_roots: tuple = field(default_factory=_allowed_roots)
    protected_paths: tuple = field(default_factory=_protected_paths)
    allow_voice_confirm: bool = _b("FRIDAY_ALLOW_VOICE_CONFIRM", False)
    confirm_timeout_s: int = _i("FRIDAY_CONFIRM_TIMEOUT_S", 45)
    automation_grant_s: int = _i("FRIDAY_AUTOMATION_GRANT_S", 300)  # how long one approval covers mouse/keyboard control
    command_timeout_s: int = _i("FRIDAY_COMMAND_TIMEOUT_S", 30)

    # ── Background agents ──────────────────────────────────────
    max_agents: int = _i("FRIDAY_MAX_AGENTS", 4)                        # simultaneous agents
    agent_max_rounds: int = _i("FRIDAY_AGENT_MAX_ROUNDS", 20)           # model calls per agent before it must stop
    agent_timeout_s: int = _i("FRIDAY_AGENT_TIMEOUT_S", 900)            # default lifetime cap per agent
    agent_api_concurrency: int = _i("FRIDAY_AGENT_API_CONCURRENCY", 2)  # simultaneous model requests across ALL agents

    # ── Presence (home/away) ────────────────────────────────────
    presence_phone_mac: str = _s("FRIDAY_PHONE_MAC")            # your phone's Wi-Fi MAC, e.g. AA:BB:CC:DD:EE:FF
    presence_phone_ip: str = _s("FRIDAY_PHONE_IP")              # optional: its usual IP, just used to nudge an ARP refresh
    presence_check_interval_s: float = _f("FRIDAY_HOME_CHECK_INTERVAL_S", 120.0)
    presence_manual_hold_s: float = _f("FRIDAY_PRESENCE_MANUAL_HOLD_S", 4 * 3600)

    # ── Phone call (Twilio Voice) ────────────────────────────────
    call_from: str = _s("TWILIO_CALL_FROM")                    # blank = reuse TWILIO_WHATSAPP_FROM
    call_to: str = _s("FRIDAY_CALL_TO")                         # E.164, e.g. +353xxxxxxxxx
    call_voice: str = _s("FRIDAY_CALL_VOICE", "alice")          # Twilio's built-in voice; no add-on needed
    call_max_per_day: int = _i("FRIDAY_CALL_MAX_PER_DAY", 6)

    # ── Free push (ntfy) ─────────────────────────────────────────
    ntfy_server: str = _s("FRIDAY_NTFY_SERVER", "https://ntfy.sh")
    ntfy_topic: str = _s("FRIDAY_NTFY_TOPIC")

    # ── Background agents ──────────────────────────────────────
    max_agents: int = _i("FRIDAY_MAX_AGENTS", 4)                        # simultaneous agents
    agent_max_rounds: int = _i("FRIDAY_AGENT_MAX_ROUNDS", 20)           # model calls per agent before it must stop
    agent_timeout_s: int = _i("FRIDAY_AGENT_TIMEOUT_S", 900)            # default lifetime cap per agent
    agent_api_concurrency: int = _i("FRIDAY_AGENT_API_CONCURRENCY", 2)  # simultaneous model requests across ALL agents

    # ── Derived paths ──────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return self.data_dir / "friday_memory.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def faces_dir(self) -> Path:
        return self.data_dir / "faces"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.snapshot_dir, self.faces_dir, self.trash_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── Convenience ────────────────────────────────────────────
    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.twilio_sid and self.twilio_token and self.twilio_whatsapp_from and self.whatsapp_to)

    @property
    def firetv_configured(self) -> bool:
        return bool(self.firetv_host)

    def problems(self) -> list[str]:
        """Human-readable setup warnings shown at startup."""
        out: list[str] = []
        if not self.api_key:
            out.append("DIFFUSIONGEMMA_API_KEY is not set — I have no brain until it is.")
        if self.tts_engine == "fish" and not self.fish_api_key:
            out.append("Fish Audio selected but FISH_API_KEY is empty — falling back to the local voice.")
        if not self.whatsapp_configured:
            out.append("WhatsApp alerts not configured (Twilio settings missing).")
        if not self.firetv_configured:
            out.append("Fire TV not configured (FIRETV_HOST missing).")
        if not self.presence_phone_mac:
            out.append("Presence detection not configured (FRIDAY_PHONE_MAC missing) — security alerts can't tell if you're away.")
        if not (self.twilio_sid and self.twilio_token and self.call_to):
            out.append("Phone-call alerts not configured (needs Twilio + FRIDAY_CALL_TO).")
        if not self.ntfy_topic:
            out.append("Free push alerts not configured (FRIDAY_NTFY_TOPIC missing).")
        return out


settings = Settings()
