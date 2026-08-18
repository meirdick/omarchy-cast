#!/usr/bin/env python3
"""The helper's pure parsers, against output captured from real devices.

Run with any python3; none of this imports pychromecast, pyatv or
androidtvremote2, because the parsers must be testable on a machine with no
backend installed at all.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "helper", os.path.join(HERE, "..", "bin", "omarchy-cast-helper.py"))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(label)
        print("FAIL  %s  %s" % (label, detail))


def eq(label, actual, expected):
    check(label, actual == expected, "got %r want %r" % (actual, expected))


def read(name):
    with open(os.path.join(HERE, "..", "fixtures", name)) as handle:
        return handle.read()


print("=== avahi googlecast, captured from this network ===")
devices = helper.parse_avahi(read("avahi-googlecast.txt"))
eq("one device, not one per address family", len(devices), 1)
den = devices[0]
eq("friendly name comes from fn, not the service name", den["name"], "Den TV")
eq("model", den["model"], "Google TV Streamer")
eq("ipv4 address", den["host"], "192.168.2.140")
eq("id is normalized", den["id"], "cast:767a74054b35365ddef41664cdcc2089")
eq("the receiver status text is the app", den["app"], "YouTube")
# st=1 says an app is running, never that it is playing. Reporting PLAYING
# here would put a play icon on a paused TV, which is why this path reports
# UNKNOWN and lets the real backend refine it.
eq("running but not known to be playing", den["state"], "UNKNOWN")
eq("no controls without a real backend", any(den["can"].values()), False)
eq("and says why", den["detail"], "discovery only")

print("=== avahi android tv remote ===")
atvs = helper.parse_avahi_atv(read("avahi-androidtvremote2.txt"))
eq("one remote", len(atvs), 1)
eq("name is unescaped", atvs[0]["name"], "Den TV")
eq("host", atvs[0]["host"], "192.168.2.140")
eq("pairing port", atvs[0]["port"], 6466)

print("=== avahi airplay ===")
airplay = helper.parse_avahi_airplay(read("avahi-airplay.txt"))
eq("one receiver", len(airplay), 1)
eq("name is unescaped", airplay[0]["name"], "Samsung 7 Series (55)")
eq("host", airplay[0]["host"], "192.168.2.13")
eq("port", airplay[0]["port"], 44239)
eq("model from the txt record", airplay[0]["model"], "UTU700D")
# deviceid is a MAC, and it is what pyatv uses as its identifier too, so a
# device found here and one found by pyatv are recognisably the same.
eq("identity is the deviceid", airplay[0]["id"], "BC:7E:8B:34:AC:CB")

print("=== avahi escaping ===")
eq("spaces", helper.avahi_unescape("Den\\032TV"), "Den TV")
eq("parentheses", helper.avahi_unescape("Samsung\\0407\\041"), "Samsung(7)")
eq("nothing to do", helper.avahi_unescape("plain"), "plain")
eq("none is empty", helper.avahi_unescape(None), "")

print("=== garbage in ===")
for junk in ["", "not avahi output", "=;;;;\n", "+;wlp0s20f3;IPv4;x;y;local\n",
             "=;a;IPv4;n;t;local;h\n", None]:
    try:
        helper.parse_avahi(junk)
        helper.parse_avahi_atv(junk)
        helper.parse_avahi_airplay(junk)
    except Exception as exc:
        check("parsers survive %r" % (junk,), False, str(exc))

print("=== uuid normalization ===")
# pychromecast reports a dashed uuid and mDNS an undashed one for the same
# device. They are never live at once, but switching between them must not
# look like a different device.
eq("dashes removed",
   helper.normalize_uuid("767a7405-4b35-365d-def4-1664cdcc2089"),
   "767a74054b35365ddef41664cdcc2089")
eq("already flat", helper.normalize_uuid("767A74054B35365D"), "767a74054b35365d")
eq("none", helper.normalize_uuid(None), "")

print("=== numeric coercion ===")
eq("nan is refused", helper.as_float(float("nan"), 7.0), 7.0)
eq("infinity is refused", helper.as_float(float("inf"), 7.0), 7.0)
eq("none falls back", helper.as_float(None, 3.0), 3.0)
eq("a string number works", helper.as_float("2.5"), 2.5)
eq("nonsense falls back", helper.as_float("abc", 1.0), 1.0)

print("=== the record is always complete ===")
rec = helper.device_record(id="cast:x", state="NONSENSE")
eq("an unknown state is not passed through", rec["state"], "UNKNOWN")
for field in ("id", "name", "kind", "app", "title", "artist", "position",
              "duration", "volume", "can", "at"):
    check("record has %s" % field, field in rec)
for control in ("pause", "seek", "next", "prev", "stop", "volume", "mute",
                "keys", "power"):
    check("can.%s defaults to false" % control, rec["can"][control] is False)

print("=== capability bitmask ===")
# The live read from the Google TV Streamer was 207.
class FakeStatus:
    supported_media_commands = 207

backend = helper.CastBackend()
can = backend._capabilities(FakeStatus())
eq("pause", can["pause"], True)
eq("seek", can["seek"], True)
eq("stream volume", can["volume"], True)
eq("stream mute", can["mute"], True)
eq("queue next", can["next"], True)
eq("queue prev", can["prev"], True)

class NoCommands:
    supported_media_commands = 0

none_can = backend._capabilities(NoCommands())
eq("a zero mask enables nothing", any(none_can.values()), False)

print("=== deduplication ===")
sent = []
original_emit = helper.emit
helper.emit = lambda obj: sent.append(obj)
try:
    one = helper.device_record(id="cast:dup", state="PLAYING", position=10.0, at=1.0)
    two = helper.device_record(id="cast:dup", state="PLAYING", position=10.2, at=2.0)
    three = helper.device_record(id="cast:dup", state="PAUSED", position=10.2, at=3.0)
    helper.emit_device(one)
    helper.emit_device(two)
    helper.emit_device(three)
    eq("an identical record is not re-sent", len(sent), 2)
    eq("but a state change is", sent[1]["state"], "PAUSED")
    helper.forget_device("cast:dup")
    helper.emit_device(three)
    eq("a device that left and came back is sent again", len(sent), 3)
finally:
    helper.emit = original_emit

print("\nOK" if not failures else "\n%d FAILURES" % len(failures))
sys.exit(0 if not failures else 1)
