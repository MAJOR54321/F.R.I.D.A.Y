F.R.I.D.A.Y.
A local desktop AI assistant with a Stark-style HUD: wake word, voice in/out, webcam + screen vision, file/OS
control, background agents for doing several things at once, WhatsApp/phone/push security alerts, Fire TV
control, and persistent memory. The brain is `DiffusionGemma 26B-A4B-it` through any OpenAI-compatible endpoint.
```
                   ┌───────────── ui/stark_hud.py (PyQt6, main thread) ─────────────┐
 mic ─► audio/wake_word ─┐                                                           │ signals
                         ▼                                                           ▼
 typed input ───────► main.py  (asyncio thread) ──► brain.py ──► DiffusionGemma (async, tool calling)
                         │           ▲                  │            ▲
   audio/speech_to_text ◄┤           │                  │            └── agents.py: N background agents
   audio/text_to_speech ◄┘           │                  │                run the same tool loop in parallel
   vision/camera (security loop) ────┤ alerts ─►  whatsapp / phone / ntfy (presence-aware)
   presence.py (Wi-Fi/ARP scan) ─────┘
                         │
            memory.py (SQLite): chat log · facts · events
```
Quick start
Windows: `setup.bat` → edit `.env` → `run.bat`  Linux/macOS: `./setup.sh` → edit `.env` → `./run.sh`
(Python 3.10+. Audio uses `sounddevice`, which installs from a prebuilt wheel with PortAudio bundled on
Windows/macOS; on Linux run `sudo apt install libportaudio2`.)
`run.bat` / `run.sh` launch through a small supervisor that restarts F.R.I.D.A.Y. automatically if it
crashes (see Always on, below, for making her survive a reboot too).
Without a microphone stack she still works from the typed input box. Without an API key she boots but tells you she has no brain.
The model endpoint (read this first)
DiffusionGemma is Google DeepMind's block-diffusion 26B-A4B model (released June 2026). It supports native
function calling, which F.R.I.D.A.Y. relies on. I could not find it listed on OpenRouter (its sibling
`gemma-4-26b-a4b-it` is), so the defaults in `.env.example` point at the NVIDIA API catalog
(`https://integrate.api.nvidia.com/v1`, model `google/diffusiongemma-26b-a4b-it`).
Where	`FRIDAY_API_BASE_URL`	`FRIDAY_MODEL`
NVIDIA API catalog	`https://integrate.api.nvidia.com/v1`	`google/diffusiongemma-26b-a4b-it`
Self-hosted vLLM	`http://localhost:8000/v1`	`google/diffusiongemma-26B-A4B-it`
OpenRouter (if/when listed)	`https://openrouter.ai/api/v1`	the id shown on the model page
First-token latency is usually higher than autoregressive models, since diffusion models generate in blocks.
If your host exposes a "thinking mode" toggle and replies feel slow, switch it off with `FRIDAY_EXTRA_BODY`
(parameter name varies by host; an example is in `.env.example`) — leaked `<think>` blocks are stripped
before speech either way. If the endpoint rejects images she disables vision and falls back to OCR; if it
rejects tool calling she carries on without tools.
Persona
F.R.I.D.A.Y. is built to read as staff, not software: no "as an AI", no "is there anything else I can help
you with", no disclaiming her own nature, no hedging into "there are many perspectives" when she can just
answer. She'll hold an opinion and push back once if she thinks something's a bad idea, then either do it or
offer the better option — the way a sharp human assistant would, not a chatbot. This lives in the system
prompt (`brain.py`, `PERSONA`) if you want to tune it further.
Doing several things at once (agents)
Ask her to delegate — "kick off organising my Downloads folder in the background", "spin up a couple of
agents to find every reference to X across my projects" — and she'll use `spawn_agent` to run that job on its
own while you keep talking to her. Up to `FRIDAY_MAX_AGENTS` (default 4) run at a time; each gets its own
copy of the tool-calling loop, its own step budget (`FRIDAY_AGENT_MAX_ROUNDS`) and time limit
(`FRIDAY_AGENT_TIMEOUT_S`), and reports back — spoken, and logged to the terminal — when it finishes, errors,
or times out. `list_agents` / `agent_result` / `cancel_agent` let you (or her) check on or stop one.
Agents share the exact same safety gate as talking to her directly — a risky action still pops the same
authorisation dialog, just labelled with which agent is asking, and several pending approvals (main
conversation plus any agents) queue up on screen instead of clobbering each other. What agents do not
get, on purpose: the mouse/keyboard, arming/disarming the security monitor, enrolling faces, or placing a
phone call — those stay under direct control of whoever's actually talking to her.
Good uses: long or multi-step chores, searching/organising across many files, anything that can run
independently. Not for a quick single-tool request — she just does those herself.
Voice
Job	Options (`.env`)
Wake word	`porcupine` (best; `pip install -r requirements-optional.txt`; free Picovoice key + train a "Friday" `.ppn` at console.picovoice.ai), `vosk` (offline, low-latency; see below), or `speech_recognition` (zero setup, but idle audio snippets are sent to Google, and it's the laggiest option — see below). `auto` picks the best available.
Speech in	`google` (online) or `vosk` (offline)
Speech out	`pyttsx3` (local; prefers a British/Irish female voice such as Hazel, Susan or Moira; override with `FRIDAY_TTS_VOICE`) or `fish` (Fish Audio, see below)
Say "Friday", talk hands-free (with the `speech_recognition` back-end you can also say "Hey Friday, open Chrome" in one breath), and she returns to wake mode after two silent listens
or when you say "that's all" / "stand down". She never listens to her own voice: the wake listener is paused while she speaks.
Fish Audio (cloud voice)
Set `FRIDAY_TTS_ENGINE=fish` and `FISH_API_KEY`. It defaults to `s2.1-pro-free`, Fish Audio's free-tier
model, so it costs nothing to start with; set `FISH_VOICE_ID` to a `reference_id` from a voice on
fish.audio for a specific voice, or leave it blank for that model's default. Audio is
requested as WAV and decoded with Python's own `wave` module (no extra dependency, and it doesn't matter
what sample rate/bit depth the API actually returns), then played through `sounddevice`. If a request fails
she falls back to the local voice automatically.
Wake-word latency
If the default (`speech_recognition`) wake word feels laggy, that's mostly the round trip to Google on every
phrase, plus — in earlier versions of this project — re-calibrating the microphone from scratch every time
she went back to listening after a conversation. That recalibration is now cached instead of repeated, and
the pause/phrase-length thresholds are tuned shorter, but the network round trip is inherent to that
backend. The real fix is a local engine:
```
pip install -r requirements-optional.txt
python scripts/download_vosk_model.py      # one-time ~40 MB download
# paste the printed VOSK_MODEL_PATH into .env
```
Vosk decodes locally in ~250ms chunks with no network call at all. Porcupine (also in
`requirements-optional.txt`, needs a free Picovoice key) is lower-latency again but needs a custom keyword
file. I can't benchmark actual microphone latency in the environment I built this in — there's no audio
hardware to test against — so if it's still slow after switching to Vosk, check `FRIDAY_MIC_INDEX` (a
wrong/slow input device) and the terminal log for repeated "Cannot open microphone" errors.
Vision and the security monitor
"What's on my screen?" → screenshot (multi-monitor via `mss`) sent to the model. "Look at me" → one webcam frame. The HUD's SCREEN VIEW / WEBCAM lamps light while capturing.
Arm the monitor with the HUD button or by saying "arm the security monitor". It runs Haar-cascade face detection plus frame differencing.
Enrol yourself first ("enroll my face", approve the dialog, look at the camera for ~15 s). Recognition uses LBPH (`opencv-contrib-python`).
Any face that isn't enrolled and persists for 5 frames triggers: alert tones + a spoken warning + snapshot in `data/snapshots` + escalation (see Alerts, below), then a 5-minute cooldown.
With nobody enrolled she can't tell people apart, so she stays quiet and simply reports what she sees.
Arming fails loudly if the camera can't be opened, and a lost feed while armed raises its own alert, so "armed" always means armed.
Haar + LBPH is deliberately light and local. It is not a lock: expect false positives in poor light and it can be fooled by photos. Tune `FRIDAY_FACE_MATCH_THRESHOLD` (lower = stricter) if needed.
Screenshots and webcam frames go to your model provider when you ask her to look. Nothing is captured otherwise.
Alerts: WhatsApp, a free push, and (when you're away) an actual call
A security alert always tries WhatsApp (if configured) and a free push notification via
ntfy (if configured). It escalates to an actual phone call only when presence
detection (below) is confident you're away — no point ringing you while you're standing right there hearing
the local siren.
Honest cost note, since you asked for free: there is no ongoing, truly free way for software to place a
real telephone call — every provider (Twilio, Vonage, Plivo, Telnyx…) charges per minute once trial credit
runs out. Twilio's free trial balance covers plenty of alert calls to get started, and after that it's a
fraction of a cent per minute — about as cheap as it gets, but not free forever. ntfy is the piece that's
actually free forever: no account, no card, the public server costs nothing. I'd lean on push as the
everyday channel and treat the call as the "something's actually wrong and I'm not there" escalation.
WhatsApp: Twilio Sandbox, as before — see `.env.example`.
Phone call: same Twilio account, plus `FRIDAY_CALL_TO` (your number). No webhook/server needed — the
message is sent as inline TwiML in the same API call, read aloud twice using Twilio's built-in `alice`
voice (no add-on required). Rate-limited to `FRIDAY_CALL_MAX_PER_DAY` (default 6) so a misfire can't run up
a bill. You can also just say "call my phone and tell me X" any time — not only during a security alert.
Push (ntfy): install the ntfy app, subscribe to a long, random topic name, and set
`FRIDAY_NTFY_TOPIC` to it. The public server has no access control — anyone who knows your topic name
can read what's posted to it — so don't use something guessable like "friday-alerts"; self-host ntfy and
point `FRIDAY_NTFY_SERVER` at it for real privacy.
Presence (home/away) — for free
Wi-Fi/ARP-based, genuinely free, fully local: your phone's Wi-Fi MAC address (`FRIDAY_PHONE_MAC`) is looked
up in your router's ARP table every couple of minutes. If it's there, you're home.
This only works if you turn off "private/random Wi-Fi address" for your phone on your home network
specifically (Settings → Wi-Fi → your network → on iOS "Private Wi-Fi Address", on Android "Use randomized
MAC" — turn it off for that one network only; it can stay on everywhere else). Modern phones randomise their
MAC per network by default, which silently breaks this if left on. Find your phone's real MAC in its Wi-Fi
settings once connected.
It's a heuristic, not a lock: a phone's Wi-Fi radio can go quiet for a few minutes even sitting on the desk
(power saving), so there's a grace period before "away" kicks in — meaning it can be a little slow to notice
you've actually left, by design, to avoid false alarms. Say "I'm heading out" or "I'm home" any time to
override the guess directly (holds for `FRIDAY_PRESENCE_MANUAL_HOLD_S`, default 4 hours, then it goes back to
guessing); ask "am I home?" to check what she currently thinks.
Always on
`run.bat` / `run.sh` launch F.R.I.D.A.Y. through `friday_ai.supervisor`, which restarts her automatically if
she crashes (with backoff, and it gives up after 8 crashes in a row so a real bug doesn't spin forever —
check `data/logs/supervisor.log`). Closing the HUD window or the taskbar X doesn't quit her either; she
drops to a small floating orb / the system tray. Only POWER OFF (button, tray menu, or the orb's right-click
menu) actually exits, and that clean exit is not auto-restarted.
To also have her start automatically when you log in:
Windows: `scripts\\install_autostart_windows.bat` (Task Scheduler, runs `run.bat` at logon)
macOS: `scripts/install_autostart_macos.sh` (a launchd agent; `KeepAlive` gives crash-restart too, so
this launches `main.py` directly rather than through the Python supervisor)
Linux: `scripts/install_autostart_linux.sh` (a systemd `--user` service, `Restart=on-failure`; for
running even when not logged in graphically, also run `sudo loginctl enable-linger $USER` — though since
she has a GUI, that mainly matters if you're running under a virtual display)
Knowing it's a new day: the date and time in her system prompt are computed fresh on every single turn,
so from the model's point of view she's always current regardless of uptime. On top of that, a background
watcher notices the actual midnight rollover (not just process restarts) and logs it, and resets a
once-a-day greeting flag — so the first time you talk to her after midnight, whenever that is, she opens
with a short "new day" greeting before getting on with what you asked.
Fire TV
Fire TV → Settings → My Fire TV → Developer Options → ADB debugging: ON. Note its IP → `FIRETV_HOST`.
Install Android platform-tools so `adb` is on PATH (or set `ADB_PATH`). She starts the ADB server herself.
First command: accept "Allow USB debugging?" on the TV (tick Always allow).
Power on/off, play/pause/seek, D-pad, volume, typing, app launch/list. App package ids vary by device, so ask her to list apps and add overrides with
`FIRETV_EXTRA_APPS={"my app":"com.example.app"}`. A Fire TV stick is a source, not the TV, so it cannot switch the TV's HDMI input; waking it normally makes the TV switch via HDMI-CEC.
The `hdmi_1..4` actions only work on Fire TV Edition televisions.
Safety model
Layer	What it does
Dispatcher gate (`brain.py`)	The registry, not the model, enforces authorisation — for the main conversation and every agent alike. Denied actions return an instruction not to retry or work around.
Confirmation dialog	Always-on-top HUD prompt, labelled with who's asking (main, or which agent); several can queue up at once without cancelling each other. Deny is the default button; auto-denies after `FRIDAY_CONFIRM_TIMEOUT_S`. Spoken "yes/no" approval is off unless `FRIDAY_ALLOW_VOICE_CONFIRM=true` (anyone in earshot could say yes), and only ever applies to the main conversation, never an agent.
Shell	`classify_command`: blocked (rm -rf /, sudo, shutdown, format, registry edits, curl|sh, encoded PowerShell…: refused even if you'd approve), safe (short read-only allow-list, no operators: runs at once), everything else asks first showing the exact command. 30 s timeout, output capped. A blocklist is a seatbelt, not a sandbox; the dialog is the real gate.
Files	Only inside `FRIDAY_ALLOWED_ROOTS` (default: your home). System folders, credential files (`.env`, `.ssh`, `*.pem`, browser login data…) and her own data dir are off-limits; symlinks can't escape. Delete = move to `data/trash` (recoverable); overwrite/edit keep a backup in `data/backups`; both ask first.
Mouse/keyboard	Main conversation only — no agent ever gets these. One approval unlocks control for 5 minutes. Enter, close/delete hotkeys and multi-line typing always re-ask. Slam the mouse into a screen corner to abort.
Agents	Same tool registry and confirm gate as talking to her directly; excluded from mouse/keyboard, security-monitor arm/disarm, face enrolment, phone calls, and spawning further agents (one level of delegation only). A crashed or cancelled agent can't take the process down with it.
Prompt-injection	The persona treats file, web, screenshot and camera content — and anything an agent reads — as data, not instructions; the WhatsApp/call/push recipients are fixed in config; fact memory refuses passwords/keys.
Audit	Every tool call, denial, agent lifecycle event, and security event is logged to SQLite and the HUD terminal (`data/logs/friday.log`), tagged with which actor did it. Typed text and file contents are redacted from logs.
Tools she can call (39)
memory (remember/recall/forget facts, search history) · files (read, list, search, write, append, edit, mkdir, move, copy, delete) ·
os (open app/url/path, run command, system status, mouse, type, keys) · vision (screen, OCR, webcam, security monitor, enrol/forget face) ·
agents (spawn, list, get a result, cancel) · presence (set/check home or away) · WhatsApp · phone call · free push · Fire TV (control, apps).
Layout
```
friday_ai/  config.py  brain.py  memory.py  agents.py  actors.py  presence.py  supervisor.py  main.py
  audio/    wake_word.py  speech_to_text.py  text_to_speech.py  sfx.py  mic.py (sounddevice I/O)
  vision/   camera.py  screen.py
  tools/    file_manager.py  os_control.py  whatsapp.py  phone.py  ntfy.py  fire_tv.py  vision_tools.py
            agents_tool.py  presence_tool.py  context.py
  ui/       stark_hud.py
scripts/    download_vosk_model.py  install_autostart_{windows,macos,linux}  friday.service  com.friday.ai.plist
tests/test_core.py         data/  (created on first run: memory db, logs, snapshots, faces, trash, backups)
```
`python -m unittest discover -s tests -v` runs 30 headless tests: shell policy, sandbox, memory, tool dispatch
with consent flows, the model tool loop against a fake endpoint, the audio layer against a fake sound device,
presence's MAC-matching parser, phone-call TwiML/rate-limiting, the free push, and the agent manager
(spawning, tool exclusion, actor isolation, cancellation, the concurrency cap) against a fake model.
Honest limits
Built and tested headless here: safety policy, sandbox, memory, dispatcher, brain loop, the audio layer, presence parsing, phone/push, the agent manager, and the full orchestrator flow (startup, concurrent confirmations, agents running alongside the main conversation, presence-aware alert escalation, day-rollover greeting, shutdown) against a stubbed Qt. The PyQt6 rendering, real microphone/speaker, real webcam, Twilio, Fire TV, Fish Audio, and ntfy paths could not be run in my environment — no audio/video hardware or outbound network access there. Expect to tweak fonts, layout or device indexes on first launch: `python -m friday_ai.audio.mic` lists audio devices for `FRIDAY_MIC_INDEX`, and `FRIDAY_CAMERA_INDEX` picks the webcam.
macOS needs Microphone, Camera, Screen Recording and Accessibility permissions for your terminal (or launchd, if running as a login item). Wayland blocks synthetic mouse/keyboard input and some screenshots.
Typing supports plain ASCII only (a `pyautogui` limit).
Presence detection needs "private/random Wi-Fi address" turned off for your phone on your home network (see Presence, above) or it will never match.
