"""F.R.I.D.A.Y. — central orchestrator.

Threads
  main thread     Qt (HUD). Never blocks.
  "asyncio"       the event loop: conversation turns, tool calls, confirmations, alert handling, agents.
  wake-word       background mic listener        tts            speech worker
  security-monitor  webcam loop                  presence       Wi-Fi/ARP scan loop
  (STT / tools use short-lived worker threads via asyncio.to_thread)

Multiple things at once: the main conversation and any number of background agents (see agents.py) are each
an "actor" (actors.py) running through the same Brain.run_tool_loop. They share one confirmation queue (a
risky action always still needs a human to click something) and the same safety rules; agents just don't get
the tools that would let them grab your mouse/keyboard, flip system-wide state, or place a phone call.

Run:  python -m friday_ai.main   (or, for auto-restart-on-crash: python -m friday_ai.supervisor)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import threading
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import QApplication

from friday_ai import actors
from friday_ai.agents import AgentManager, AgentRecord
from friday_ai.audio.sfx import SoundFX
from friday_ai.audio.speech_to_text import SpeechToText
from friday_ai.audio.text_to_speech import TextToSpeech
from friday_ai.audio.wake_word import WakeWordListener
from friday_ai.brain import Brain, ToolRegistry
from friday_ai.config import Settings, settings
from friday_ai.memory import Memory
from friday_ai.presence import PresenceTracker
from friday_ai.tools import ToolContext, register_all
from friday_ai.tools.fire_tv import FireTV
from friday_ai.tools.ntfy import NtfyNotifier
from friday_ai.tools.phone import PhoneCaller
from friday_ai.tools.whatsapp import WhatsAppNotifier
from friday_ai.ui.stark_hud import QtLogHandler, StarkHUD
from friday_ai.vision.camera import Camera
from friday_ai.vision.screen import ScreenReader

_DISMISS = ("that's all", "thats all", "that will be all", "that'll do", "stand down", "go to sleep", "stop listening",
            "never mind", "nevermind", "dismissed", "thanks friday", "thank you friday")
_REDACT_TOOLS = {"type_text", "write_file", "edit_file", "append_file", "send_whatsapp", "remember_fact"}


def is_dismissal(text: str) -> bool:
    norm = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    return any(norm.startswith(p) for p in _DISMISS)


def _part_of_day() -> str:
    h = time.localtime().tm_hour
    return "morning" if h < 12 else "afternoon" if h < 18 else "evening"


class Friday:
    def __init__(self, hud: StarkHUD, loop: asyncio.AbstractEventLoop, cfg: Settings = settings):
        self.cfg, self.hud, self.loop = cfg, hud, loop
        self.log = logging.getLogger("friday.core")
        self._session_active = False
        self._muted = False
        self._busy = 0
        self._today = date.today()
        self._greeted_today = False

        self.memory = Memory(cfg.db_path)
        self.sfx = SoundFX(cfg.sound_effects)
        self.tts = TextToSpeech(cfg, on_start=self._tts_started, on_end=self._tts_ended, on_level=hud.set_level)
        self.stt = SpeechToText(cfg)
        self.screen = ScreenReader(cfg, on_capture=self._screen_status)
        self.camera = Camera(cfg, on_alert=self._camera_alert, on_status=self._camera_status, on_info=hud.set_security_text)
        self.whatsapp = WhatsAppNotifier(cfg)
        self.firetv = FireTV(cfg)
        self.presence = PresenceTracker(cfg, on_change=self._presence_changed)
        self.phone = PhoneCaller(cfg)
        self.ntfy = NtfyNotifier(cfg)

        self.registry = ToolRegistry(confirm=self.confirm, on_event=self._tool_event)
        self.brain = Brain(cfg, self.memory, self.registry)
        self.brain.status_hook = lambda: self.agents.status_line() if hasattr(self, "agents") else ""
        self.agents = AgentManager(cfg, self.brain, self.registry, on_done=self._on_agent_done)
        register_all(self.registry, ToolContext(cfg, self.memory, self.confirm, self.camera, self.screen,
                                                self.whatsapp, self.firetv, self.set_security,
                                                self.agents, self.presence, self.phone, self.ntfy))
        self.wake = WakeWordListener(cfg, on_wake=self._on_wake, on_status=self._wake_status)
        self.log.info("%d tools registered", len(self.registry))

    # ───────────────────────── helpers ─────────────────────────
    def _submit(self, coro) -> "asyncio.futures.Future":
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f) -> None:
            if not f.cancelled() and f.exception():
                self.log.error("Background task failed: %r", f.exception(), exc_info=f.exception())

        fut.add_done_callback(_done)
        return fut

    def _idle_state(self) -> str:
        return "muted" if self._muted else "idle"

    def _hint(self) -> str:
        return "Wake word muted — use ACTIVATE or type below." if self._muted else "Say “Friday” — or type below."

    # ───────────────────────── startup / shutdown ─────────────────────────
    async def startup(self) -> None:
        cfg, hud, t = self.cfg, self.hud, self.cfg.user_title
        hud.set_state("thinking")
        hud.log("SYSTEM", "F.R.I.D.A.Y. boot sequence initiated")
        self.sfx.play("boot")
        self.memory.log_event("boot", "startup")
        self.memory.prune_messages()
        await asyncio.to_thread(self.tts.warm)  # pay pyttsx3's one-off init cost now, not on the first reply

        hud.set_indicator("mic", "on" if self.stt.available else "error")
        hud.set_indicator("voice", "on")
        hud.set_indicator("screen", "on" if self.screen.available else "off")
        hud.set_indicator("cam", "off" if self.camera.available else "error")
        hud.set_indicator("link", "busy" if cfg.api_key else "warn")
        hud.set_indicator("whatsapp", "on" if self.whatsapp.configured else "off")
        hud.set_indicator("firetv", "warn" if self.firetv.configured else "off")
        hud.set_indicator("agents", "off")
        hud.set_indicator("presence", "warn" if self.presence.configured else "off")
        hud.set_security_text("SECURITY: DISARMED")
        for problem in cfg.problems():
            hud.log("WARNING", problem)
        if not self.stt.available:
            hud.log("WARNING", "Microphone stack unavailable (needs SpeechRecognition + sounddevice). Typed input still works.")

        wake_ok = self.wake.start()
        hud.set_indicator("wake", "on" if wake_ok else "off")
        if cfg.security_autostart:
            await asyncio.to_thread(self.set_security, True)
        self.presence.start()

        if cfg.api_key:
            asyncio.create_task(self._check_link())
        asyncio.create_task(self._day_watcher())

        part = _part_of_day()
        if not cfg.api_key:
            line = f"Good {part}, {t}. I'm online, but with no API key I've no brain. Set DIFFUSIONGEMMA_API_KEY and restart me."
        else:
            line = f"Good {part}, {t}. F.R.I.D.A.Y. online. All systems nominal."
            if not wake_ok:
                line += " The wake word listener isn't available, so use the activate button or type."
        hud.say("F.R.I.D.A.Y.", line)
        hud.set_state(self._idle_state())
        hud.set_status(self._hint())
        self.tts.speak(line)
        self._greeted_today = True  # the line above already covered today; the day watcher resets this at midnight

    async def _check_link(self) -> None:
        ok = await self.brain.ping()
        self.hud.set_indicator("link", "on" if ok else "error")
        self.hud.log("SYSTEM" if ok else "ERROR", "Model endpoint reachable." if ok else
                     f"Model endpoint check failed ({self.cfg.api_base_url}, model {self.cfg.model}).")

    async def _day_watcher(self) -> None:
        """Runs for as long as the process does, so a genuinely 'always on' F.R.I.D.A.Y. notices midnight
        even though main.py's normal startup greeting only fires once per process launch."""
        while True:
            now = time.localtime()
            seconds_to_midnight = (24 - now.tm_hour - 1) * 3600 + (60 - now.tm_min - 1) * 60 + (60 - now.tm_sec) + 2
            await asyncio.sleep(max(30, seconds_to_midnight))
            today = date.today()
            if today != self._today:
                self._today = today
                self._greeted_today = False
                self.memory.log_event("system", "new_day", {"date": today.isoformat()})
                self.hud.log("SYSTEM", f"New day: {today.strftime('%A %d %B %Y')}.")

    def shutdown(self) -> None:
        self.log.info("Shutting down…")
        for step in (self.camera.stop_monitor, self.wake.stop, self.presence.stop, self.tts.shutdown, self.sfx.close):
            try:
                step()
            except Exception:  # noqa: BLE001
                self.log.debug("shutdown step failed", exc_info=True)
        for rec in self.agents.list():
            if rec.status == "running":
                self.agents.cancel(rec.id)
        try:
            self._submit(self.brain.close()).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        self.memory.log_event("boot", "shutdown")
        self.memory.close()

    # ───────────────────────── HUD → core slots (GUI thread; keep them tiny) ─────────────────────────
    def on_text(self, text: str) -> None:
        self._submit(self.handle_utterance(text, "text"))

    def on_activate(self) -> None:
        if not self.stt.available:
            self.hud.log("WARNING", "No microphone stack installed — type your request instead.")
            return
        self._submit(self.voice_session(None))

    def on_mute(self, muted: bool) -> None:
        self._muted = muted
        self.wake.set_muted(muted)
        self.hud.set_indicator("wake", "warn" if muted else "on")
        if not self._session_active and self._busy == 0:
            self.hud.set_state(self._idle_state())
        self.hud.set_status(self._hint())

    def on_security_toggle(self, armed: bool) -> None:
        self._submit(asyncio.to_thread(self.set_security, armed))

    # ───────────────────────── conversation ─────────────────────────
    def _on_wake(self, trailing: str) -> None:  # wake-word thread
        self._submit(self.voice_session(trailing or None))

    async def voice_session(self, first_utterance: Optional[str]) -> None:
        """One wake-up: converse hands-free until the user goes quiet or dismisses us."""
        if self._session_active:
            return
        self._session_active = True
        t = self.cfg.user_title
        silences = 0
        utterance = first_utterance
        try:
            self.wake.pause()
            self.sfx.play("wake")
            self.hud.set_indicator("mic", "busy")
            if not utterance:
                self.hud.set_state("listening")
                await self.tts.say(random.choice([f"Yes, {t}?", f"Go ahead, {t}.", f"I'm listening, {t}.", f"At your service, {t}."]))
            while True:
                if not utterance:
                    self.hud.set_state("listening")
                    self.hud.set_status("Listening…")
                    heard = await asyncio.to_thread(self.stt.listen_once)
                    if not heard:  # None = silence, "" = unintelligible
                        silences += 1
                        if silences >= self.cfg.session_max_silences:
                            break
                        continue
                    utterance = heard
                silences = 0
                if is_dismissal(utterance):
                    self.hud.say("YOU", utterance)
                    await self.tts.say(f"Standing by, {t}.")
                    break
                await self.handle_utterance(utterance, "voice")
                utterance = None
        finally:
            self._session_active = False
            self.hud.set_indicator("mic", "on" if self.stt.available else "error")
            if self._busy == 0:
                self.hud.set_state(self._idle_state())
            self.hud.set_status(self._hint())
            self.wake.resume()

    async def _maybe_greet_new_day(self) -> None:
        if self._greeted_today:
            return
        self._greeted_today = True
        line = f"Morning, {self.cfg.user_title}. New day — {time.strftime('%A %d %B').lstrip('0')}."
        self.hud.say("F.R.I.D.A.Y.", line)
        await self.tts.say(line)

    async def handle_utterance(self, text: str, source: str = "voice") -> None:
        self._busy += 1
        try:
            await self._maybe_greet_new_day()
            self.hud.say("YOU", text)
            self.hud.set_state("thinking")
            self.hud.set_status("Processing…")
            reply = await self.brain.respond(text, source=source)
            self.hud.say("F.R.I.D.A.Y.", reply)
            if source == "voice" or self.cfg.speak_typed_replies:
                await self.tts.say(reply)
        except Exception:  # noqa: BLE001
            self.log.exception("Turn failed")
            self.sfx.play("error")
        finally:
            self._busy -= 1
            if not self._session_active and self._busy == 0:
                self.hud.set_state(self._idle_state())
                self.hud.set_status(self._hint())

    # ───────────────────────── TTS / wake callbacks (other threads) ─────────────────────────
    def _tts_started(self) -> None:
        self.wake.pause()  # don't let her hear her own name
        self.hud.set_state("speaking")
        self.hud.set_indicator("voice", "busy")

    def _tts_ended(self) -> None:
        self.hud.set_indicator("voice", "on")
        if not self._session_active:  # during a session the loop owns wake/HUD state
            self.wake.resume()
            if self._busy == 0:
                self.hud.set_state(self._idle_state())

    def _wake_status(self, status: str) -> None:
        self.hud.set_indicator("wake", {"listening": "on", "paused": "busy", "muted": "warn", "error": "error"}.get(status, "off"))

    # ───────────────────────── authorisation ─────────────────────────
    async def confirm(self, message: str, tool: str = "") -> bool:
        """Ask the user to approve a risky action. Approval comes from the HUD dialog (or, if enabled, a spoken
        yes/no). Several actors — the main conversation, any running agents — can each have a request pending
        at once; the HUD queues them and shows one at a time, each labelled with who's asking."""
        t = self.cfg.user_title
        actor = actors.current_actor.get()
        if actor != actors.MAIN:
            message = f"[Background agent — {self.agents.label(actor)}]\n{message}"
        self.log.warning("Authorisation requested [%s / %s]: %s", actor, tool or "-", message.splitlines()[0])
        self.sfx.play("confirm")
        if actor == actors.MAIN:
            self.hud.say("F.R.I.D.A.Y.", f"Authorisation required:\n{message}")
        rid, fut = self.hud.request_confirmation(message, self.cfg.confirm_timeout_s)
        spoken = None
        if actor == actors.MAIN:  # only interrupt with speech for the conversation the user is actually in
            spoken = self.tts.speak(f"{t}, I need your authorisation." + (" Say yes or no, or use the screen." if self.cfg.allow_voice_confirm else " Please check the screen."))

        tasks: list[asyncio.Future] = [asyncio.wrap_future(fut)]
        if spoken is not None and self.cfg.allow_voice_confirm and self.stt.available:
            tasks.append(asyncio.create_task(self._voice_confirm(spoken)))
        result = False
        try:
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=self.cfg.confirm_timeout_s + 3)
                if not done:  # safety net: the dialog should have auto-denied by now
                    break
                answer: Optional[bool] = None
                for d in done:
                    tasks.remove(d)
                    if not d.cancelled() and d.exception() is None and d.result() is not None:
                        answer = bool(d.result())
                if answer is not None:
                    result = answer
                    break
        finally:
            for pending in tasks:
                pending.cancel()
            self.hud.dismiss_confirmation(rid)
        self.sfx.play("ok" if result else "deny")
        self.hud.log("SYSTEM", "Authorised." if result else "Denied.")
        return result

    async def _voice_confirm(self, spoken_done: threading.Event) -> Optional[bool]:
        await asyncio.get_running_loop().run_in_executor(None, spoken_done.wait, 15)
        was_paused = self._session_active
        self.wake.pause()
        try:
            for _ in range(3):
                answer = await asyncio.to_thread(self.stt.listen_yes_no, 6.0)
                if answer is not None:
                    return answer
            return None
        finally:
            if not was_paused:
                self.wake.resume()

    # ───────────────────────── tools ↔ HUD ─────────────────────────
    def _tool_event(self, name: str, args: dict, status: str, detail: str) -> None:
        summary = ", ".join(
            f"{k}=<{len(str(v))} chars>" if name in _REDACT_TOOLS or len(str(v)) > 80 else f"{k}={v!r}"
            for k, v in args.items())
        actor = actors.current_actor.get()
        tag = "" if actor == actors.MAIN else f"[{actor.split(':', 1)[-1]}] "
        self.hud.log("TOOL", f"{tag}{name}({summary}) → {status}" + (f": {detail}" if detail else ""))
        if status in {"ok", "error", "refused", "denied"}:
            self.memory.log_event("tool", f"{actor}:{name}: {status}", {"args": summary})
        if name.startswith("firetv") and status in {"ok", "error", "refused"}:
            self.hud.set_indicator("firetv", "on" if status == "ok" else "warn")
        if name == "send_whatsapp" and status in {"ok", "error", "refused"}:
            self.hud.set_indicator("whatsapp", "on" if status == "ok" else "warn")

    # ───────────────────────── background agents ─────────────────────────
    def _on_agent_done(self, rec: AgentRecord) -> None:  # runs on the asyncio loop (agent's own task)
        self.hud.set_indicator("agents", "busy" if self.agents.running_count() > 0 else "off")
        icon = {"done": "SYSTEM", "error": "ERROR", "timeout": "WARNING"}.get(rec.status, "SYSTEM")
        self.hud.log(icon, f"Agent [{rec.id}] {rec.status}: {rec.label()}")
        note = f"Background task finished — {rec.label()}: {rec.result or '(no report)'}"
        self.memory.add_message("assistant", note, source="agent")
        self.memory.log_event("agent", f"{rec.id} {rec.status}", {"goal": rec.goal, "result": rec.result})
        self.hud.say("F.R.I.D.A.Y.", note)
        if self._busy == 0 and not self._session_active:
            self.tts.speak(note)

    # ───────────────────────── vision / security ─────────────────────────
    def set_security(self, enabled: bool) -> bool:
        """Arm/disarm the webcam security monitor. Blocking; safe to call from any thread."""
        if enabled:
            self.camera.start_monitor()
        else:
            self.camera.stop_monitor()
        armed = self.camera.monitoring
        self.hud.set_indicator("security", "on" if armed else "off")
        self.hud.set_security_button(armed)
        self.memory.log_event("security", "monitor armed" if armed else "monitor disarmed")
        self.hud.log("SYSTEM", "Security monitor ARMED." if armed else "Security monitor disarmed.")
        return armed

    def _camera_status(self, status: str) -> None:
        self.hud.set_indicator("cam", {"capturing": "busy", "monitoring": "on", "idle": "off", "error": "error"}.get(status, "off"))
        if status == "capturing":
            self.sfx.play("camera")

    def _screen_status(self, status: str) -> None:
        self.hud.set_indicator("screen", "busy" if status == "capturing" else "on")

    def _presence_changed(self, is_home: bool) -> None:  # presence thread
        self.hud.set_indicator("presence", "on" if is_home else "warn")
        self.hud.log("SYSTEM", f"Presence: {'home' if is_home else 'away'}.")

    def _camera_alert(self, kind: str, message: str, snapshot: str) -> None:  # camera thread
        self._submit(self._handle_alert(kind, message, snapshot))

    async def _handle_alert(self, kind: str, message: str, snapshot: str) -> None:
        t = self.cfg.user_title
        self.memory.log_event("security", message, {"kind": kind, "snapshot": snapshot})
        self.hud.set_state("alert")
        snap_name = Path(snapshot).name if snapshot else ""
        self.hud.log("WARNING", message + (f" — snapshot: {snap_name}" if snap_name else ""))
        self.hud.say("F.R.I.D.A.Y.", message + (". Snapshot saved." if snap_name else "."))
        self.sfx.play("alert")
        spoken = {"unrecognised_person": f"{t}, unrecognised individual at your workstation.",
                  "camera_fault": f"{t}, I've lost the camera feed."}.get(kind, f"{t}, security anomaly detected.")
        self.tts.speak(spoken)

        away = self.presence.is_home() is False  # only escalate to a call when we're confident you're NOT there
        if self.whatsapp.configured:
            try:
                await asyncio.to_thread(self.whatsapp.send_alert, message, f"Snapshot saved on the PC: {snap_name}" if snap_name else "")
                self.hud.log("SYSTEM", "WhatsApp alert sent.")
                self.hud.set_indicator("whatsapp", "on")
            except Exception as e:  # noqa: BLE001
                self.hud.log("ERROR", f"WhatsApp alert failed: {e}")
                self.hud.set_indicator("whatsapp", "error")
        else:
            self.hud.log("WARNING", "WhatsApp not configured — alert was local only.")

        if self.ntfy.configured:
            try:
                await asyncio.to_thread(self.ntfy.push, "F.R.I.D.A.Y. security alert", message, True)
                self.hud.log("SYSTEM", "Push alert sent.")
            except Exception as e:  # noqa: BLE001
                self.hud.log("ERROR", f"Push alert failed: {e}")

        if away and self.phone.configured:
            try:
                await asyncio.to_thread(self.phone.call, f"{message}. This is an automated call from F.R.I.D.A.Y. because you appear to be away from home.")
                self.hud.log("SYSTEM", "Phone call placed (you're marked away).")
            except Exception as e:  # noqa: BLE001
                self.hud.log("ERROR", f"Phone call failed: {e}")
        elif away:
            self.hud.log("WARNING", "You're marked away but phone-call alerts aren't configured (Twilio + FRIDAY_CALL_TO).")

        await asyncio.sleep(6)
        if not self._session_active and self._busy == 0:
            self.hud.set_state(self._idle_state())


# ───────────────────────────── bootstrap ─────────────────────────────
def setup_logging(cfg: Settings) -> None:
    cfg.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(cfg.log_dir / "friday.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [file_handler, console]
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _hook(exc_type, exc, tb) -> None:
        logging.getLogger("friday").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
    threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> int:
    setup_logging(settings)
    app = QApplication(sys.argv)
    app.setApplicationName("F.R.I.D.A.Y.")
    app.setQuitOnLastWindowClosed(False)  # the orb / tray keep her alive when the HUD is hidden

    hud = StarkHUD()
    logging.getLogger().addHandler(QtLogHandler(hud))

    loop = asyncio.new_event_loop()
    threading.Thread(target=_run_loop, args=(loop,), daemon=True, name="asyncio").start()

    friday = Friday(hud, loop)
    hud.text_submitted.connect(friday.on_text)
    hud.activate_requested.connect(friday.on_activate)
    hud.mute_toggled.connect(friday.on_mute)
    hud.security_toggled.connect(friday.on_security_toggle)

    def power_off() -> None:
        hud.force_quit()
        app.quit()

    hud.quit_requested.connect(power_off)
    app.aboutToQuit.connect(friday.shutdown)

    hud.show()
    friday._submit(friday.startup())  # noqa: SLF001
    code = app.exec()
    loop.call_soon_threadsafe(loop.stop)
    return code


if __name__ == "__main__":
    sys.exit(main())
