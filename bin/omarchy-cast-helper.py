#!/usr/bin/env python3
"""Resident bridge between the Omarchy shell and media devices on the network.

Reads commands as newline-delimited JSON on stdin, writes device state as
newline-delimited JSON on stdout. One line in, zero or more lines out. stdout
carries nothing but JSON; everything else goes to stderr.

The shell cannot speak any of these protocols itself. QML has no HTTP client,
no WebSocket module, and no way to reach a protobuf-over-TLS channel, so this
process exists to turn three unrelated device protocols into one flat record
the bar can render without knowing anything about any of them.

Nothing in here may raise out to the top level. A device that misbehaves, a
backend whose library is missing, a network that vanishes mid-read: all of
those are normal, and the process must survive every one of them, because the
shell restarting it is visible to the user as a bar widget flickering.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

HELPER_VERSION = "1.0.0"

# Poll interval for the dependency-free avahi fallback. The real backends push,
# so this timer only exists for the degraded path.
AVAHI_INTERVAL = 10.0

# Reconnect backoff, seconds. Devices sleep, reboot for firmware, and drop off
# wifi; none of that deserves a tight retry loop.
BACKOFF_START = 1.0
BACKOFF_CAP = 30.0


# --------------------------------------------------------------------- output

_out_lock = threading.Lock()


def emit(obj):
    """Write one JSON object to stdout. Never raises."""
    try:
        line = json.dumps(obj, separators=(",", ":"), default=str)
    except Exception:
        return
    try:
        with _out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except Exception:
        # stdout is gone, which means the shell dropped us. Nothing to do.
        pass


def log(*parts):
    try:
        sys.stderr.write("cast-helper: " + " ".join(str(p) for p in parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def log_exc(context):
    log(context, "\n" + traceback.format_exc())


# ---------------------------------------------------------------- the record
#
# Every backend normalizes to this one shape. Nothing above this file ever sees
# a pychromecast object, a pyatv Playing instance, or an avahi TXT record.
#
#   id          stable identity, "<kind>:<uuid>". Never an IP or a friendly
#               name; both change and both are reused across devices.
#   state       IDLE | BUFFERING | PLAYING | PAUSED | OFFLINE | UNKNOWN
#   position    seconds, sampled at `at`. The panel extrapolates from these two
#               rather than being told a new position every second.
#   at          time.time() when position was sampled.
#   can         which controls to enable. Derived from the device's own
#               capability report, never assumed from the protocol.

STATES = ("IDLE", "BUFFERING", "PLAYING", "PAUSED", "OFFLINE", "UNKNOWN")


def device_record(**kw):
    rec = {
        "type": "device",
        "id": "",
        "name": "",
        "kind": "",
        "model": "",
        "host": "",
        "app": "",
        "state": "UNKNOWN",
        "title": "",
        "artist": "",
        "album": "",
        "art": "",
        "position": 0.0,
        "duration": 0.0,
        "rate": 1.0,
        "at": time.time(),
        "volume": -1.0,
        "muted": False,
        "volumeFixed": False,
        "paired": True,
        "detail": "",
        "can": {
            "pause": False,
            "seek": False,
            "next": False,
            "prev": False,
            "stop": False,
            "volume": False,
            "mute": False,
            "keys": False,
            "power": False,
        },
    }
    can = kw.pop("can", None)
    rec.update(kw)
    if can:
        rec["can"].update(can)
    if rec["state"] not in STATES:
        rec["state"] = "UNKNOWN"
    return rec


_last_seen = {}
_last_lock = threading.Lock()


def emit_device(rec):
    """Emit a device record, unless it says exactly what the last one said.

    Connecting to a Cast device fires connection, receiver and media callbacks
    within a few milliseconds of each other, and every app or volume change
    fires several more. Forwarding all of them makes the bar rebuild its text
    repeatedly for one real event. Position is compared at one-second
    granularity because the panel extrapolates between updates anyway, so a
    sub-second difference is not news.
    """
    try:
        fingerprint = json.dumps(
            {k: (round(v, 0) if k == "position" and isinstance(v, float) else v)
             for k, v in rec.items() if k != "at"},
            sort_keys=True, separators=(",", ":"), default=str,
        )
    except Exception:
        fingerprint = None

    if fingerprint is not None:
        with _last_lock:
            if _last_seen.get(rec.get("id")) == fingerprint:
                return
            _last_seen[rec.get("id")] = fingerprint
    emit(rec)


def forget_device(dev_id):
    with _last_lock:
        _last_seen.pop(dev_id, None)


def as_float(value, fallback=0.0):
    try:
        if value is None:
            return fallback
        out = float(value)
        # NaN and infinity both survive float() and both poison JSON.
        if out != out or out in (float("inf"), float("-inf")):
            return fallback
        return out
    except (TypeError, ValueError):
        return fallback


def normalize_uuid(value):
    """One spelling of a device id across every backend.

    pychromecast reports a dashed UUID and the mDNS TXT record an undashed
    one for the same device. They are never both live at once, but a user
    switching between them should not see the widget treat it as a new
    device and lose its place.
    """
    return re.sub(r"[^a-z0-9]", "", as_text(value).lower())


def as_text(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


# ------------------------------------------------------------ backend contract
#
# Every backend implements the same three methods, so adding a protocol never
# touches the shell or the QML:
#
#   available()  -> (ok, detail)   can this backend run at all
#   start()                        begin discovery; emit device records
#   command(cmd, dev_id, payload)  act on one device
#   stop()                         tear down


class Backend:
    kind = "none"

    def available(self):
        return False, "not implemented"

    def start(self):
        pass

    def command(self, cmd, dev_id, payload):
        return False, "unsupported"

    def stop(self):
        pass


# ------------------------------------------------------------- Google Cast
#
# The interesting one. The receiver pushes RECEIVER_STATUS and MEDIA_STATUS
# unsolicited, so there is no polling here at all: pychromecast holds one TLS
# socket per device, answers the PING/PONG heartbeat, follows the transport id
# when the running app changes, and calls back into us.
#
# Two things about this protocol that are easy to get wrong and that this class
# handles explicitly:
#
#   * A Google TV wraps *native* app playback in a synthetic cast session
#     (appType ANDROID_TV). So a device nobody is casting to still reports what
#     it is playing. That is the whole reason this widget is useful.
#
#   * That same synthetic session reports metadataType 0 with no images array,
#     so artwork is frequently absent. That is normal, not an error, and the
#     record carries an empty `art` rather than a broken URL.


class CastBackend(Backend):
    kind = "cast"

    def __init__(self):
        self._last = {}
        self._pc = None
        self._zc = None
        self._browser = None
        self._casts = {}          # uuid str -> Chromecast
        self._info = {}           # uuid str -> discovery info dict
        self._lock = threading.RLock()
        self._stopping = False

    def available(self):
        try:
            import pychromecast  # noqa: F401
        except Exception as exc:
            return False, "python-pychromecast not installed (%s)" % exc
        return True, ""

    # ----------------------------------------------------------- discovery

    def start(self):
        import pychromecast
        import zeroconf

        self._pc = pychromecast
        self._zc = zeroconf.Zeroconf()

        listener = pychromecast.discovery.SimpleCastListener(
            add_callback=self._on_found,
            remove_callback=self._on_lost,
            update_callback=self._on_found,
        )
        self._browser = pychromecast.discovery.CastBrowser(listener, self._zc)
        self._browser.start_discovery()
        log("cast discovery started")

    @staticmethod
    def _key_for(uuid):
        return normalize_uuid(uuid)

    def _on_found(self, uuid, service):
        # Runs on a zeroconf thread. Hand the slow part to our own thread so we
        # never block discovery, which would stall every other device too.
        threading.Thread(
            target=self._connect_guarded, args=(uuid,), daemon=True
        ).start()

    def _on_lost(self, uuid, service, cast_info):
        key = self._key_for(uuid)
        with self._lock:
            cast = self._casts.pop(key, None)
            self._info.pop(key, None)
        if cast is not None:
            try:
                cast.disconnect(blocking=False)
            except Exception:
                pass
        forget_device("cast:" + key)
        emit({"type": "gone", "id": "cast:" + key})

    def _connect_guarded(self, uuid):
        try:
            self._connect(uuid)
        except Exception:
            log_exc("cast connect %s" % uuid)

    def _connect(self, uuid):
        key = self._key_for(uuid)
        with self._lock:
            if self._stopping or key in self._casts:
                return
            info = self._browser.devices.get(uuid)
            if info is None:
                return
            # Claim the slot before the slow connect so two discovery
            # callbacks for the same device cannot both build a Chromecast.
            self._casts[key] = None

        try:
            cast = self._pc.get_chromecast_from_cast_info(info, self._zc)
        except Exception:
            with self._lock:
                self._casts.pop(key, None)
            raise

        with self._lock:
            self._casts[key] = cast
            self._info[key] = {
                "name": as_text(getattr(info, "friendly_name", "")),
                "model": as_text(getattr(info, "model_name", "")),
                "host": as_text(getattr(info, "host", "")),
                "port": getattr(info, "port", 8009),
            }

        watcher = _CastWatcher(self, key)
        try:
            cast.register_connection_listener(watcher)
            cast.register_status_listener(watcher)
            cast.media_controller.register_status_listener(watcher)
        except Exception:
            log_exc("cast listener registration %s" % key)

        # wait() drives the socket client until the CONNECT handshake and the
        # first RECEIVER_STATUS have landed. It is bounded so a device that
        # accepts TCP but never answers cannot hold this thread forever.
        try:
            cast.wait(timeout=15)
        except Exception:
            log("cast wait timed out for %s" % key)
        self.publish(key)

    # ------------------------------------------------------------ publishing

    def publish(self, key):
        try:
            self._publish(key)
        except Exception:
            log_exc("cast publish %s" % key)

    def _publish(self, key):
        with self._lock:
            cast = self._casts.get(key)
            info = dict(self._info.get(key) or {})
        if cast is None:
            return

        status = getattr(cast, "status", None)
        media = None
        try:
            media = cast.media_controller.status
        except Exception:
            media = None

        app = ""
        volume = -1.0
        muted = False
        fixed = False
        standby = False
        if status is not None:
            app = as_text(getattr(status, "display_name", "")) or as_text(
                getattr(status, "status_text", "")
            )
            volume = as_float(getattr(status, "volume_level", -1.0), -1.0)
            muted = bool(getattr(status, "volume_muted", False))
            # 'fixed' means the TV or HDMI-CEC owns the volume and SET_VOLUME on
            # the receiver channel is silently ignored. The panel needs to know
            # so it can route volume elsewhere instead of lying to the user.
            fixed = as_text(getattr(status, "volume_control_type", "")) == "fixed"
            standby = bool(getattr(status, "is_stand_by", False))

        state = "IDLE"
        title = artist = album = art = ""
        position = duration = 0.0
        rate = 1.0
        can = {}

        if media is not None:
            state = as_text(getattr(media, "player_state", "")) or "UNKNOWN"
            title = as_text(getattr(media, "title", ""))
            artist = as_text(getattr(media, "artist", ""))
            album = as_text(getattr(media, "album_name", ""))
            position = as_float(getattr(media, "current_time", 0.0))
            duration = as_float(getattr(media, "duration", 0.0))
            rate = as_float(getattr(media, "playback_rate", 1.0), 1.0) or 1.0
            art = self._best_image(media)
            can = self._capabilities(media)

        if standby and state in ("IDLE", "UNKNOWN"):
            state = "IDLE"

        can.setdefault("volume", volume >= 0.0)
        can.setdefault("mute", volume >= 0.0)
        can["stop"] = bool(app)

        emit_device(device_record(
            id="cast:" + key,
            kind="cast",
            name=info.get("name", ""),
            model=info.get("model", ""),
            host=info.get("host", ""),
            app=app,
            state=state,
            title=title,
            artist=artist,
            album=album,
            art=art,
            position=position,
            duration=duration,
            rate=rate,
            at=time.time(),
            volume=volume,
            muted=muted,
            volumeFixed=fixed,
            can=can,
        ))

    def _best_image(self, media):
        """Largest artwork URL, or "" when the app supplies none.

        Android-TV-native sessions routinely report metadataType 0 with no
        images at all, so an empty string here is the common case and the
        panel must render without it.
        """
        try:
            images = list(getattr(media, "images", None) or [])
        except Exception:
            return ""
        best = ""
        best_area = -1
        for img in images:
            url = as_text(getattr(img, "url", "") or (img.get("url") if isinstance(img, dict) else ""))
            if not url.startswith(("http://", "https://")):
                continue
            width = as_float(getattr(img, "width", 0) or 0)
            height = as_float(getattr(img, "height", 0) or 0)
            area = width * height
            if area > best_area:
                best_area, best = area, url
        return best

    def _capabilities(self, media):
        """Decode what the receiver says it will accept.

        pychromecast exposes named properties over the supportedMediaCommands
        bitmask; fall back to decoding the mask by hand if a future version
        renames them. Buttons are enabled from this and never from a guess
        about what the protocol allows.
        """
        names = {
            "pause": "supports_pause",
            "seek": "supports_seek",
            "volume": "supports_stream_volume",
            "mute": "supports_stream_mute",
            "next": "supports_queue_next",
            "prev": "supports_queue_prev",
        }
        can = {}
        for key, attr in names.items():
            value = getattr(media, attr, None)
            if isinstance(value, bool):
                can[key] = value
        if len(can) == len(names):
            return can

        mask = int(as_float(getattr(media, "supported_media_commands", 0)))
        bits = {"pause": 1, "seek": 2, "volume": 4, "mute": 8,
                "next": 64, "prev": 128}
        for key, bit in bits.items():
            can.setdefault(key, bool(mask & bit))
        return can

    # -------------------------------------------------------------- commands

    def command(self, cmd, dev_id, payload):
        key = dev_id.split(":", 1)[1] if ":" in dev_id else dev_id
        with self._lock:
            cast = self._casts.get(key)
        if cast is None:
            return False, "device not connected"

        mc = cast.media_controller
        try:
            if cmd == "playPause":
                status = getattr(mc, "status", None)
                playing = as_text(getattr(status, "player_state", "")) == "PLAYING"
                mc.pause() if playing else mc.play()
            elif cmd == "play":
                mc.play()
            elif cmd == "pause":
                mc.pause()
            elif cmd == "stop":
                mc.stop()
            elif cmd == "next":
                mc.queue_next()
            elif cmd == "previous":
                mc.queue_prev()
            elif cmd == "seek":
                mc.seek(as_float(payload.get("position")))
            elif cmd == "volume":
                level = max(0.0, min(1.0, as_float(payload.get("level"))))
                ok, detail = self._set_volume(cast, mc, level=level)
                threading.Timer(0.35, self.publish, args=(key,)).start()
                return ok, detail
            elif cmd == "mute":
                ok, detail = self._set_volume(
                    cast, mc, muted=bool(payload.get("muted", True)))
                threading.Timer(0.35, self.publish, args=(key,)).start()
                return ok, detail
            elif cmd in ("volumeUp", "volumeDown"):
                # Receiver-level stepping. Works on devices that own their own
                # volume scale but refuse to be told an absolute level.
                (cast.volume_up if cmd == "volumeUp" else cast.volume_down)()
            elif cmd == "quit":
                cast.quit_app()
            elif cmd == "refresh":
                mc.update_status()
            else:
                return False, "unknown command"
        except Exception as exc:
            return False, str(exc)

        # The receiver will push a MEDIA_STATUS of its own, but echoing our
        # optimistic view immediately keeps the button from feeling dead.
        threading.Timer(0.35, self.publish, args=(key,)).start()
        return True, ""

    def _set_volume(self, cast, mc, level=None, muted=None):
        """Change volume, by whichever channel this device will accept.

        Two channels exist and neither works everywhere:

          * The media namespace honours SET_VOLUME when the running app
            advertises STREAM_VOLUME in supportedMediaCommands. This is the one
            that works on a TV, because it adjusts the *stream* rather than the
            device.
          * The receiver channel adjusts the device, and is what a speaker
            wants — but a TV-attached receiver reports controlType "fixed" and
            drops the command silently, because the TV and HDMI-CEC own the
            real volume.

        pychromecast only wraps the receiver channel, so the media path is sent
        as a raw message on the media namespace. Silently doing nothing is the
        worst outcome here, so when neither channel will take it this returns
        an error the panel can show instead.
        """
        status = getattr(mc, "status", None)
        volume = {}
        if level is not None:
            volume["level"] = level
        if muted is not None:
            volume["muted"] = muted

        wants = "volume" if level is not None else "mute"
        supported = getattr(
            status, "supports_stream_volume" if wants == "volume" else "supports_stream_mute",
            False,
        )
        session = getattr(status, "media_session_id", None)

        if supported and session is not None:
            try:
                mc.send_message({"type": "SET_VOLUME", "volume": volume},
                                inc_session_id=True)
                return True, ""
            except Exception as exc:
                log("media-namespace volume failed: %s" % exc)

        receiver = getattr(getattr(cast, "socket_client", None),
                           "receiver_controller", None)
        fixed = as_text(getattr(getattr(cast, "status", None),
                                "volume_control_type", "")) == "fixed"
        if receiver is not None and not fixed:
            try:
                if level is not None:
                    receiver.set_volume(level)
                if muted is not None:
                    receiver.set_volume_muted(muted)
                return True, ""
            except Exception as exc:
                return False, str(exc)

        if fixed:
            return False, ("this device's volume is controlled by the TV; "
                           "pair the Android TV remote to change it")
        return False, "this device does not accept volume commands"

    def stop(self):
        self._stopping = True
        with self._lock:
            casts = list(self._casts.values())
            self._casts.clear()
        for cast in casts:
            try:
                if cast is not None:
                    cast.disconnect(blocking=False)
            except Exception:
                pass
        for closer in (self._browser, self._zc):
            try:
                if closer is self._browser and closer is not None:
                    closer.stop_discovery()
                elif closer is not None:
                    closer.close()
            except Exception:
                pass


class _CastWatcher:
    """Listener triple for one device.

    pychromecast 14 requires load_media_failed on the media listener; omitting
    it makes registration fail outright rather than degrade.
    """

    def __init__(self, backend, key):
        self._backend = backend
        self._key = key

    def new_cast_status(self, status):
        self._backend.publish(self._key)

    def new_media_status(self, status):
        self._backend.publish(self._key)

    def load_media_failed(self, queue_item_id, error_code):
        emit({"type": "error", "id": "cast:" + self._key,
              "message": "load failed (%s)" % error_code})

    def new_connection_status(self, status):
        state = as_text(getattr(status, "status", ""))
        if state in ("LOST", "DISCONNECTED", "FAILED"):
            emit_device(device_record(id="cast:" + self._key, kind="cast",
                                      state="OFFLINE", detail=state))
        else:
            self._backend.publish(self._key)


# --------------------------------------------------------- avahi fallback
#
# The dependency-free path. A Cast device's mDNS TXT record already carries the
# friendly name, the model, whether an app is running, and the receiver's
# status text — which in practice is the app name, "YouTube" or similar. That
# is enough for a bar line before anything is installed, so the widget is
# useful on a machine with no python-pychromecast rather than being invisible.
#
# It cannot control anything and it has no track metadata. It exists so the
# degraded state is "less information" instead of "broken".


def avahi_unescape(text):
    """Undo avahi-browse's \\nnn decimal escaping."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text or "")


def parse_avahi(stdout):
    """Turn `avahi-browse -rpt _googlecast._tcp` output into device records.

    Kept a plain function taking a string so the parser is testable against a
    captured fixture without a network or an avahi daemon.
    """
    devices = {}
    for line in (stdout or "").splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 10:
            continue
        proto, name, host, addr, port, txt = (
            parts[2], parts[3], parts[6], parts[7], parts[8], parts[9]
        )
        # Each device answers on both stacks; one record per device is enough
        # and IPv4 is the one everything else here can reach.
        if proto != "IPv4":
            continue
        fields = {}
        for chunk in re.findall(r'"([^"]*)"', txt):
            if "=" in chunk:
                k, _, v = chunk.partition("=")
                fields[k] = v
        uuid = normalize_uuid(fields.get("id", "") or avahi_unescape(name))
        if not uuid:
            continue
        friendly = avahi_unescape(fields.get("fn", "") or name)
        app = avahi_unescape(fields.get("rs", ""))
        running = fields.get("st", "0") == "1"
        devices[uuid] = device_record(
            id="cast:" + uuid,
            kind="cast",
            name=friendly,
            model=avahi_unescape(fields.get("md", "")),
            host=addr,
            app=app,
            # st only says an app is running, never whether it is playing.
            # Claiming PLAYING here would put a play icon on a paused TV, so
            # this reports the honest answer and lets the real backend refine
            # it when one is installed.
            state="UNKNOWN" if running else "IDLE",
            at=time.time(),
            detail="discovery only",
        )
    return list(devices.values())


class AvahiBackend(Backend):
    kind = "avahi"

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()

    def available(self):
        if not shutil.which("avahi-browse"):
            return False, "avahi-browse not found"
        return True, ""

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        seen = set()
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    ["avahi-browse", "-rpt", "_googlecast._tcp"],
                    capture_output=True, text=True, timeout=15,
                )
                records = parse_avahi(proc.stdout)
                current = set()
                for rec in records:
                    current.add(rec["id"])
                    emit_device(rec)
                for gone in seen - current:
                    forget_device(gone)
                    emit({"type": "gone", "id": gone})
                seen = current
            except Exception:
                log_exc("avahi sweep")
            self._stop.wait(AVAHI_INTERVAL)

    def command(self, cmd, dev_id, payload):
        return False, "install python-pychromecast to control this device"

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------ asyncio island
#
# pychromecast is thread-based; pyatv and androidtvremote2 are asyncio. Rather
# than force one model on the other, the async backends share a single event
# loop running on its own thread, and everything crossing the boundary goes
# through submit().


class AsyncIsland:
    def __init__(self):
        self.loop = None
        self._ready = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        import asyncio

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        except Exception:
            log_exc("async loop")

    def submit(self, coro):
        """Schedule a coroutine. Returns a concurrent Future, or None."""
        import asyncio

        if self.loop is None:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception:
            log_exc("submit")
            return None

    def stop(self):
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass


ISLAND = AsyncIsland()


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "omarchy", "meirdick.cast")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def state_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, "omarchy", "meirdick.cast")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except Exception:
        pass
    return path


# ------------------------------------------------------------------- AirPlay
#
# Covers the Samsung TV on this network, and any Apple TV or HomePod. pyatv
# hands artwork over as bytes rather than a URL, so it is written to the cache
# directory and the record carries a file path the QML Image can load directly.
#
# Non-Apple AirPlay receivers vary a lot in how much metadata they publish.
# Every field is treated as optional for that reason.


class AirPlayBackend(Backend):
    kind = "airplay"

    def __init__(self):
        self._atvs = {}
        self._configs = {}
        self._pairings = {}
        self._lock = threading.RLock()
        self._stop = False

    def available(self):
        try:
            import pyatv  # noqa: F401
        except Exception as exc:
            return False, "pyatv not installed (%s)" % exc
        return True, ""

    def start(self):
        ISLAND.start()
        ISLAND.submit(self._scan_forever())

    async def _scan_forever(self):
        import asyncio

        while not self._stop:
            try:
                await self._scan_once()
            except Exception:
                log_exc("airplay scan")
            await asyncio.sleep(30)

    async def _scan_once(self):
        """Find receivers through avahi, then ask pyatv about each by address.

        pyatv's own multicast scan is unreliable here. A TV in standby stops
        answering probes while avahi still holds a perfectly good cached
        advertisement for it, so a discovery pass that trusts pyatv alone makes
        the device vanish from the list every time the screen goes off. Avahi
        decides what exists; pyatv is asked only how to talk to it.
        """
        import asyncio
        from ipaddress import ip_address

        import pyatv

        loop = asyncio.get_running_loop()
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ["avahi-browse", "-rpt", "_airplay._tcp"],
                capture_output=True, text=True, timeout=15,
            )
            advertised = parse_avahi_airplay(proc.stdout)
        except Exception:
            log_exc("airplay avahi sweep")
            advertised = []

        for found in advertised:
            ident = found["id"]
            with self._lock:
                if ident in self._atvs and self._atvs[ident] is not None:
                    continue

            conf = None
            try:
                probed = await pyatv.scan(
                    loop, hosts=[ip_address(found["host"])], timeout=5)
                conf = probed[0] if probed else None
            except Exception as exc:
                log("airplay probe of %s failed: %s" % (found["host"], exc))

            if conf is None:
                # Advertised but not answering: asleep, or refusing probes.
                # It is still a device the user owns and will want to see.
                self._announce_unpaired(found, "asleep or not responding")
                continue

            with self._lock:
                self._configs[ident] = conf
                self._atvs[ident] = None
            try:
                await self._connect(conf, ident)
            except Exception as exc:
                with self._lock:
                    self._atvs.pop(ident, None)
                # Most AirPlay receivers, every recent Samsung and LG included,
                # require pairing before they accept a connection. Dropping the
                # device here would hide it and leave no way to pair it.
                log("airplay %s needs pairing: %s" % (ident, exc))
                self._announce_unpaired(found, "needs pairing")

    def _announce_unpaired(self, found, reason):
        emit_device(device_record(
            id="airplay:" + found["id"],
            kind="airplay",
            name=found.get("name", "") or found["id"],
            model=found.get("model", ""),
            host=found.get("host", ""),
            state="IDLE",
            at=time.time(),
            paired=False,
            detail=reason,
        ))

    async def _connect(self, conf, ident):
        import asyncio
        import pyatv

        loop = asyncio.get_running_loop()
        # Stored credentials, written only by an explicit pair command.
        creds = self._load_credentials(ident)
        if creds:
            for proto_name, value in creds.items():
                try:
                    proto = getattr(pyatv.const.Protocol, proto_name, None)
                    if proto is not None:
                        conf.set_credentials(proto, value)
                except Exception:
                    pass

        atv = await pyatv.connect(conf, loop)
        with self._lock:
            self._atvs[ident] = atv

        name = as_text(getattr(conf, "name", "")) or ident
        listener = _AirPlayWatcher(self, ident, name)
        try:
            atv.push_updater.listener = listener
            atv.push_updater.start()
        except Exception:
            # Receivers without a push interface still answer a direct poll.
            log("airplay %s has no push updater" % ident)
        await self.publish(ident, name)

    async def publish(self, ident, name=""):
        try:
            await self._publish(ident, name)
        except Exception:
            log_exc("airplay publish %s" % ident)

    async def _publish(self, ident, name=""):
        with self._lock:
            atv = self._atvs.get(ident)
        if atv is None:
            return

        playing = None
        try:
            playing = await atv.metadata.playing()
        except Exception:
            playing = None

        state = "IDLE"
        title = artist = album = ""
        position = duration = 0.0
        if playing is not None:
            state = self._state_of(playing)
            title = as_text(getattr(playing, "title", "") or "")
            artist = as_text(getattr(playing, "artist", "") or "")
            album = as_text(getattr(playing, "album", "") or "")
            position = as_float(getattr(playing, "position", 0))
            duration = as_float(getattr(playing, "total_time", 0))

        volume = -1.0
        try:
            volume = as_float(getattr(atv.audio, "volume", -1.0), -1.0) / 100.0
        except Exception:
            volume = -1.0

        art = await self._artwork(atv, ident, title, artist)
        features = self._features(atv)

        emit_device(device_record(
            id="airplay:" + ident,
            kind="airplay",
            name=name or ident,
            state=state,
            title=title,
            artist=artist,
            album=album,
            art=art,
            position=position,
            duration=duration,
            at=time.time(),
            volume=volume,
            can=features,
        ))

    def _state_of(self, playing):
        try:
            import pyatv

            mapping = {
                pyatv.const.DeviceState.Idle: "IDLE",
                pyatv.const.DeviceState.Loading: "BUFFERING",
                pyatv.const.DeviceState.Paused: "PAUSED",
                pyatv.const.DeviceState.Playing: "PLAYING",
                pyatv.const.DeviceState.Stopped: "IDLE",
                pyatv.const.DeviceState.Seeking: "BUFFERING",
            }
            return mapping.get(getattr(playing, "device_state", None), "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def _features(self, atv):
        """Ask pyatv which controls this particular receiver supports."""
        try:
            import pyatv

            fs = atv.features
            avail = pyatv.const.FeatureState.Available

            def has(name):
                feat = getattr(pyatv.const.FeatureName, name, None)
                if feat is None:
                    return False
                return fs.get_feature(feat).state == avail

            return {
                "pause": has("Pause"), "seek": has("SetPosition"),
                "next": has("Next"), "prev": has("Previous"),
                "stop": has("Stop"), "volume": has("SetVolume"),
                "mute": has("SetVolume"),
            }
        except Exception:
            return {"pause": True, "next": True, "prev": True}

    async def _artwork(self, atv, ident, title, artist):
        """Cache artwork bytes to a file and return its path.

        pyatv gives bytes, and QML wants something it can load. The filename is
        keyed on the track so a changed track invalidates it without needing a
        cache-expiry policy.
        """
        if not (title or artist):
            return ""
        try:
            stamp = re.sub(r"[^A-Za-z0-9]+", "-", (ident + "-" + title + "-" + artist))[:120]
            path = os.path.join(cache_dir(), stamp + ".img")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
            art = await atv.metadata.artwork(width=512, height=512)
            data = getattr(art, "bytes", None) if art else None
            if not data:
                return ""
            tmp = path + ".part"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
            return path
        except Exception:
            return ""

    def _cred_path(self, ident):
        safe = re.sub(r"[^A-Za-z0-9]+", "-", ident)[:80]
        return os.path.join(state_dir(), "airplay-%s.json" % safe)

    def _load_credentials(self, ident):
        try:
            with open(self._cred_path(ident), "r") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def command(self, cmd, dev_id, payload):
        ident = dev_id.split(":", 1)[1] if ":" in dev_id else dev_id
        if cmd == "pair":
            future = ISLAND.submit(self._pair(ident, as_text(payload.get("code", ""))))
            if future is None:
                return False, "async loop unavailable"
            try:
                return future.result(timeout=30)
            except Exception as exc:
                return False, str(exc)

        with self._lock:
            atv = self._atvs.get(ident)
        if atv is None:
            return False, "device not connected — pair it first"
        future = ISLAND.submit(self._command(atv, ident, cmd, payload))
        if future is None:
            return False, "async loop unavailable"
        try:
            return future.result(timeout=8)
        except Exception as exc:
            return False, str(exc)

    async def _pair(self, ident, code):
        """Two-step AirPlay pairing, driven entirely by the user.

        Step one asks the receiver to show a PIN; step two sends back what the
        user typed and stores the credentials it returns. Nothing here runs on
        its own — an unpaired device sits in the list until someone presses
        enter on it.
        """
        import pyatv

        with self._lock:
            conf = self._configs.get(ident)
            pending = self._pairings.get(ident)

        if conf is None:
            return False, "device not found"

        if code == "":
            if pending is not None:
                try:
                    await pending.close()
                except Exception:
                    pass
            import asyncio

            loop = asyncio.get_running_loop()
            pairing = await pyatv.pair(conf, pyatv.const.Protocol.AirPlay, loop)
            await pairing.begin()
            with self._lock:
                self._pairings[ident] = pairing
            if not getattr(pairing, "device_provides_pin", True):
                # Some receivers want us to display a PIN instead. Nothing in
                # the panel can show one, so this is reported rather than
                # guessed at.
                emit({"type": "pairing", "id": "airplay:" + ident,
                      "state": "failed",
                      "message": "this receiver expects the PIN to come from "
                                 "the sender, which is not supported"})
                return False, "unsupported pairing direction"
            emit({"type": "pairing", "id": "airplay:" + ident,
                  "state": "awaiting-code"})
            return True, ""

        if pending is None:
            return False, "start pairing first"

        try:
            pending.pin(code)
            await pending.finish()
            creds = {}
            service = getattr(pending, "service", None)
            if service is not None and getattr(service, "credentials", None):
                creds["AirPlay"] = service.credentials
            if creds:
                path = self._cred_path(ident)
                with open(path, "w") as handle:
                    json.dump(creds, handle)
                os.chmod(path, 0o600)
        except Exception as exc:
            emit({"type": "pairing", "id": "airplay:" + ident,
                  "state": "failed", "message": as_text(exc)})
            return False, str(exc)
        finally:
            try:
                await pending.close()
            except Exception:
                pass
            with self._lock:
                self._pairings.pop(ident, None)

        emit({"type": "pairing", "id": "airplay:" + ident, "state": "paired"})
        with self._lock:
            self._atvs.pop(ident, None)
        try:
            await self._connect(conf, ident)
        except Exception as exc:
            return False, str(exc)
        return True, ""

    async def _command(self, atv, ident, cmd, payload):
        rc = atv.remote_control
        try:
            if cmd == "playPause":
                await rc.play_pause()
            elif cmd == "play":
                await rc.play()
            elif cmd == "pause":
                await rc.pause()
            elif cmd == "stop":
                await rc.stop()
            elif cmd == "next":
                await rc.next()
            elif cmd == "previous":
                await rc.previous()
            elif cmd == "seek":
                await rc.set_position(int(as_float(payload.get("position"))))
            elif cmd == "volume":
                level = max(0.0, min(1.0, as_float(payload.get("level"))))
                await atv.audio.set_volume(level * 100.0)
            elif cmd == "refresh":
                pass
            else:
                return False, "unknown command"
        except Exception as exc:
            return False, str(exc)
        await self.publish(ident)
        return True, ""

    def stop(self):
        self._stop = True
        with self._lock:
            atvs = [a for a in self._atvs.values() if a is not None]
            self._atvs.clear()
        for atv in atvs:
            try:
                ISLAND.submit(_close_atv(atv))
            except Exception:
                pass


async def _close_atv(atv):
    try:
        atv.close()
    except Exception:
        pass


class _AirPlayWatcher:
    def __init__(self, backend, ident, name):
        self._backend = backend
        self._ident = ident
        self._name = name

    def playstatus_update(self, updater, playstatus):
        ISLAND.submit(self._backend.publish(self._ident, self._name))

    def playstatus_error(self, updater, exception):
        emit({"type": "error", "id": "airplay:" + self._ident,
              "message": as_text(exception)})


# -------------------------------------------------------- Android TV Remote
#
# The control side-channel for a Google TV. Cast tells you what is playing;
# this sends the keys Cast cannot, which matters because a TV-attached receiver
# reports volume controlType "fixed" and ignores SET_VOLUME on the receiver
# channel.
#
# Pairing is one-time and explicitly user-driven: the TV shows a six-digit
# code, the panel sends it back, and a client certificate is written under
# $XDG_DATA_HOME with owner-only permissions. Nothing pairs on its own.
#
# Discovery reuses avahi-browse rather than standing up a second zeroconf
# instance next to the one pychromecast already runs.


def parse_avahi_atv(stdout):
    """Devices advertising _androidtvremote2._tcp, as (id, name, host, port)."""
    out = {}
    for line in (stdout or "").splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 9 or parts[2] != "IPv4":
            continue
        name = avahi_unescape(parts[3])
        host, port = parts[7], parts[8]
        if not host:
            continue
        # No stable UUID is advertised, so the hostname is the identity. It is
        # stable in practice and the record is namespaced, so a collision with
        # a cast id is impossible.
        out[host] = {"id": host, "name": name, "host": host,
                     "port": int(port) if port.isdigit() else 6466}
    return list(out.values())


def parse_avahi_airplay(stdout):
    """AirPlay receivers, as (id, name, host, port).

    Identity is the deviceid from the TXT record — a MAC address, which is what
    pyatv also uses as its identifier, so a device discovered here and one
    discovered by pyatv are recognisably the same thing.
    """
    out = {}
    for line in (stdout or "").splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 10 or parts[2] != "IPv4":
            continue
        name, host, port, txt = (avahi_unescape(parts[3]), parts[7], parts[8], parts[9])
        fields = {}
        for chunk in re.findall(r'"([^"]*)"', txt):
            if "=" in chunk:
                k, _, v = chunk.partition("=")
                fields[k] = v
        ident = fields.get("deviceid", "") or host
        if not host:
            continue
        out[ident] = {"id": ident, "name": name, "host": host,
                      "port": int(port) if port.isdigit() else 7000,
                      "model": fields.get("model", "")}
    return list(out.values())


class AndroidTVBackend(Backend):
    kind = "androidtv"

    def __init__(self):
        self._remotes = {}
        self._pairing = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()

    def available(self):
        try:
            import androidtvremote2  # noqa: F401
        except Exception as exc:
            return False, "androidtvremote2 not installed (%s)" % exc
        if not shutil.which("avahi-browse"):
            return False, "avahi-browse not found"
        return True, ""

    def start(self):
        ISLAND.start()
        threading.Thread(target=self._discover_loop, daemon=True).start()

    def _discover_loop(self):
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    ["avahi-browse", "-rpt", "_androidtvremote2._tcp"],
                    capture_output=True, text=True, timeout=15,
                )
                for found in parse_avahi_atv(proc.stdout):
                    with self._lock:
                        if found["id"] in self._remotes:
                            continue
                        self._remotes[found["id"]] = None
                    ISLAND.submit(self._connect(found))
            except Exception:
                log_exc("androidtv discovery")
            self._stop.wait(30)

    def _paths(self, ident):
        safe = re.sub(r"[^A-Za-z0-9]+", "-", ident)[:80]
        base = os.path.join(state_dir(), "atv-%s" % safe)
        return base + ".crt", base + ".key"

    async def _connect(self, found):
        from androidtvremote2 import AndroidTVRemote

        ident = found["id"]
        certfile, keyfile = self._paths(ident)
        remote = AndroidTVRemote("Omarchy Cast", certfile, keyfile, found["host"])
        try:
            await remote.async_generate_cert_if_missing()
        except Exception as exc:
            log("androidtv cert generation failed for %s: %s" % (ident, exc))
            with self._lock:
                self._remotes.pop(ident, None)
            return

        paired = True
        try:
            await remote.async_connect()
        except Exception as exc:
            # Unpaired is the expected first-run state, not a failure. Announce
            # the device so the panel can offer to pair it, and stop there.
            paired = False
            log("androidtv %s not paired: %s" % (ident, exc))

        with self._lock:
            self._remotes[ident] = remote
            found["paired"] = paired
            self._pairing[ident] = found

        if paired:
            try:
                remote.keep_reconnecting()
                remote.add_is_on_updated_callback(lambda *_: self.publish(ident))
                remote.add_current_app_updated_callback(lambda *_: self.publish(ident))
                remote.add_volume_info_updated_callback(lambda *_: self.publish(ident))
            except Exception:
                log_exc("androidtv callbacks %s" % ident)
        self.publish(ident)

    def publish(self, ident):
        try:
            self._publish(ident)
        except Exception:
            log_exc("androidtv publish %s" % ident)

    def _publish(self, ident):
        with self._lock:
            remote = self._remotes.get(ident)
            info = dict(self._pairing.get(ident) or {})
        if remote is None:
            return

        paired = bool(info.get("paired"))
        volume = -1.0
        muted = False
        if paired:
            try:
                vol = getattr(remote, "volume_info", None) or {}
                level = as_float(vol.get("level"), -1.0)
                maximum = as_float(vol.get("max"), 0.0)
                if level >= 0 and maximum > 0:
                    volume = level / maximum
                muted = bool(vol.get("muted", False))
            except Exception:
                pass

        app = ""
        on = True
        if paired:
            app = as_text(getattr(remote, "current_app", "") or "")
            on = bool(getattr(remote, "is_on", True))

        emit_device(device_record(
            id="androidtv:" + ident,
            kind="androidtv",
            name=info.get("name", ident),
            host=info.get("host", ""),
            app=app,
            # This protocol carries no track metadata at all — only keys, the
            # current app package, and volume. It is a remote, not a player,
            # and the record says so rather than inventing a state.
            state="IDLE" if not on else "UNKNOWN",
            at=time.time(),
            volume=volume,
            muted=muted,
            paired=paired,
            detail="" if paired else "needs pairing",
            can={"volume": paired, "mute": paired, "keys": paired,
                 "power": paired, "pause": paired, "next": paired,
                 "prev": paired},
        ))

    KEYS = {
        "playPause": "KEYCODE_MEDIA_PLAY_PAUSE",
        "play": "KEYCODE_MEDIA_PLAY",
        "pause": "KEYCODE_MEDIA_PAUSE",
        "stop": "KEYCODE_MEDIA_STOP",
        "next": "KEYCODE_MEDIA_NEXT",
        "previous": "KEYCODE_MEDIA_PREVIOUS",
        "volumeUp": "KEYCODE_VOLUME_UP",
        "volumeDown": "KEYCODE_VOLUME_DOWN",
        "mute": "KEYCODE_VOLUME_MUTE",
        "power": "KEYCODE_POWER",
        "home": "KEYCODE_HOME",
        "back": "KEYCODE_BACK",
        "up": "KEYCODE_DPAD_UP",
        "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT",
        "select": "KEYCODE_DPAD_CENTER",
    }

    def command(self, cmd, dev_id, payload):
        ident = dev_id.split(":", 1)[1] if ":" in dev_id else dev_id
        with self._lock:
            remote = self._remotes.get(ident)
            info = dict(self._pairing.get(ident) or {})
        if remote is None:
            return False, "device not connected"

        if cmd == "pair":
            return self._pair(remote, ident, payload)
        if not info.get("paired"):
            return False, "device is not paired"

        if cmd == "volume":
            # The protocol has no absolute volume, only steps. Nudging toward
            # the requested level is the honest approximation; the panel treats
            # this device's slider as coarse for that reason.
            target = max(0.0, min(1.0, as_float(payload.get("level"))))
            current = 0.0
            try:
                vol = getattr(remote, "volume_info", None) or {}
                maximum = as_float(vol.get("max"), 0.0)
                if maximum > 0:
                    current = as_float(vol.get("level"), 0.0) / maximum
            except Exception:
                pass
            key = "KEYCODE_VOLUME_UP" if target > current else "KEYCODE_VOLUME_DOWN"
            steps = max(1, min(10, int(abs(target - current) * 20)))
            try:
                for _ in range(steps):
                    remote.send_key_command(key)
            except Exception as exc:
                return False, str(exc)
            self.publish(ident)
            return True, ""

        key = self.KEYS.get(cmd) or (
            payload.get("key") if cmd == "key" else None
        )
        if not key:
            return False, "unknown command"
        try:
            remote.send_key_command(key)
        except Exception as exc:
            return False, str(exc)
        threading.Timer(0.4, self.publish, args=(ident,)).start()
        return True, ""

    def _pair(self, remote, ident, payload):
        code = as_text(payload.get("code", "")).strip()
        if not code:
            future = ISLAND.submit(remote.async_start_pairing())
            if future is None:
                return False, "async loop unavailable"
            try:
                future.result(timeout=15)
            except Exception as exc:
                return False, str(exc)
            emit({"type": "pairing", "id": "androidtv:" + ident,
                  "state": "awaiting-code"})
            return True, ""

        future = ISLAND.submit(remote.async_finish_pairing(code))
        if future is None:
            return False, "async loop unavailable"
        try:
            future.result(timeout=15)
        except Exception as exc:
            emit({"type": "pairing", "id": "androidtv:" + ident,
                  "state": "failed", "message": as_text(exc)})
            return False, str(exc)

        with self._lock:
            if ident in self._pairing:
                self._pairing[ident]["paired"] = True
        emit({"type": "pairing", "id": "androidtv:" + ident, "state": "paired"})
        ISLAND.submit(self._reconnect(remote, ident))
        return True, ""

    async def _reconnect(self, remote, ident):
        try:
            await remote.async_connect()
            remote.keep_reconnecting()
            remote.add_is_on_updated_callback(lambda *_: self.publish(ident))
            remote.add_current_app_updated_callback(lambda *_: self.publish(ident))
            remote.add_volume_info_updated_callback(lambda *_: self.publish(ident))
        except Exception:
            log_exc("androidtv reconnect %s" % ident)
        self.publish(ident)

    def stop(self):
        self._stop.set()
        with self._lock:
            remotes = [r for r in self._remotes.values() if r is not None]
            self._remotes.clear()
        for remote in remotes:
            try:
                remote.disconnect()
            except Exception:
                pass


# ------------------------------------------------------------------- runtime


class Helper:
    def __init__(self, enabled):
        self.enabled = enabled
        self.backends = {}
        self.missing = {}

    def probe(self):
        """Decide which backends can run, and say so once on stdout.

        The avahi fallback is deliberately mutually exclusive with the real
        Cast backend: both key devices off the same mDNS uuid, so running them
        together would have the discovery-only record overwrite live metadata a
        few seconds after every update.
        """
        candidates = [CastBackend(), AirPlayBackend(), AndroidTVBackend()]
        for backend in candidates:
            if backend.kind not in self.enabled:
                self.missing[backend.kind] = "disabled in settings"
                continue
            try:
                ok, detail = backend.available()
            except Exception as exc:
                ok, detail = False, str(exc)
            if ok:
                self.backends[backend.kind] = backend
            else:
                self.missing[backend.kind] = detail

        if "cast" not in self.backends and "avahi" in self.enabled:
            fallback = AvahiBackend()
            ok, detail = fallback.available()
            if ok:
                self.backends["avahi"] = fallback
            else:
                self.missing["avahi"] = detail

        emit({
            "type": "ready",
            "version": HELPER_VERSION,
            "backends": sorted(self.backends.keys()),
            "missing": self.missing,
            "python": "%d.%d" % sys.version_info[:2],
        })

    def start(self):
        for kind, backend in list(self.backends.items()):
            try:
                backend.start()
            except Exception as exc:
                log_exc("start %s" % kind)
                self.backends.pop(kind, None)
                self.missing[kind] = str(exc)
                emit({"type": "error", "id": "", "message":
                      "%s backend failed to start: %s" % (kind, exc)})

    def dispatch(self, message):
        cmd = as_text(message.get("cmd"))
        dev_id = as_text(message.get("id"))

        if cmd == "ping":
            emit({"type": "pong", "at": time.time()})
            return
        if cmd == "diagnose":
            emit({"type": "diagnose", "version": HELPER_VERSION,
                  "backends": sorted(self.backends.keys()),
                  "missing": self.missing})
            return
        if cmd == "refresh" and not dev_id:
            for backend in self.backends.values():
                for key in list(getattr(backend, "_casts", {}) or {}):
                    backend.publish(key)
            return

        kind = dev_id.split(":", 1)[0] if ":" in dev_id else ""
        backend = self.backends.get(kind)
        if backend is None:
            emit({"type": "error", "id": dev_id,
                  "message": "no backend for %s" % (kind or "unknown device")})
            return

        try:
            ok, detail = backend.command(cmd, dev_id, message)
        except Exception as exc:
            log_exc("command %s" % cmd)
            ok, detail = False, str(exc)
        if not ok:
            emit({"type": "error", "id": dev_id, "message": detail})

    def stop(self):
        for backend in self.backends.values():
            try:
                backend.stop()
            except Exception:
                pass
        ISLAND.stop()


def main():
    argv = sys.argv[1:]
    enabled = {"cast", "airplay", "androidtv", "avahi"}
    for arg in argv:
        if arg.startswith("--only="):
            enabled = {p.strip() for p in arg.split("=", 1)[1].split(",") if p.strip()}
        elif arg.startswith("--without="):
            enabled -= {p.strip() for p in arg.split("=", 1)[1].split(",")}
        elif arg in ("-h", "--help"):
            sys.stderr.write(__doc__ + "\nOptions: --only=a,b  --without=a,b\n")
            return 0

    helper = Helper(enabled)
    helper.probe()
    helper.start()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                emit({"type": "error", "id": "", "message": "bad json on stdin"})
                continue
            if not isinstance(message, dict):
                continue
            try:
                helper.dispatch(message)
            except Exception:
                log_exc("dispatch")
    except KeyboardInterrupt:
        pass
    except Exception:
        log_exc("stdin loop")
    finally:
        helper.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log_exc("fatal")
        sys.exit(1)
