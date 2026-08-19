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

print("=== range parsing ===")
# A receiver seeks by asking for a byte range. A server that ignores Range
# answers with the whole file from zero, so the scrub bar appears to work and
# playback silently jumps back to the start. These are the shapes that arrive.
SIZE = 1000
eq("no header means the whole file", helper.parse_range(None, SIZE), None)
eq("empty header", helper.parse_range("", SIZE), None)
eq("open ended", helper.parse_range("bytes=500-", SIZE), (500, 999))
eq("closed", helper.parse_range("bytes=100-199", SIZE), (100, 199))
eq("from zero", helper.parse_range("bytes=0-0", SIZE), (0, 0))
eq("suffix means the last n bytes", helper.parse_range("bytes=-300", SIZE), (700, 999))
eq("a suffix longer than the file clamps", helper.parse_range("bytes=-5000", SIZE), (0, 999))
eq("an end past the file clamps", helper.parse_range("bytes=900-9999", SIZE), (900, 999))
eq("whitespace is tolerated", helper.parse_range(" bytes = 10-20 ".replace(" = ", "="), SIZE), (10, 20))
eq("case", helper.parse_range("BYTES=10-20", SIZE), (10, 20))
# Well-formed but outside the file needs a 416, not a 200 with the whole file.
eq("start past the end is unsatisfiable", helper.parse_range("bytes=1000-", SIZE), "unsatisfiable")
eq("zero-length suffix is unsatisfiable", helper.parse_range("bytes=-0", SIZE), "unsatisfiable")
# Malformed, or more than this server promises: fall back to the whole file.
eq("multi-range is declined", helper.parse_range("bytes=0-99,200-299", SIZE), None)
eq("backwards", helper.parse_range("bytes=500-100", SIZE), None)
eq("not bytes", helper.parse_range("items=0-99", SIZE), None)
eq("no dash", helper.parse_range("bytes=100", SIZE), None)
eq("garbage", helper.parse_range("bytes=abc-def", SIZE), None)
eq("bare dash", helper.parse_range("bytes=-", SIZE), None)
eq("an empty file has no ranges", helper.parse_range("bytes=0-10", 0), None)

print("=== content types ===")
# Cast refuses to play something served as application/octet-stream even when
# it could decode the bytes, so guessing has to land on a real media type.
eq("mp4", helper.guess_mime("/x/a.mp4"), "video/mp4")
eq("mkv", helper.guess_mime("/x/a.mkv"), "video/x-matroska")
eq("webm", helper.guess_mime("/x/a.webm"), "video/webm")
eq("mp3", helper.guess_mime("/x/a.mp3"), "audio/mpeg")
eq("flac", helper.guess_mime("/x/a.flac"), "audio/flac")
eq("case is ignored", helper.guess_mime("/x/A.MP4"), "video/mp4")
eq("an unknown extension still names a media type",
   helper.guess_mime("/x/a.qqq"), "video/mp4")

print("=== codec warnings ===")
eq("h264 and aac are fine",
   helper.codec_warning({"video": "h264", "audio": "aac"}), "")
check("an odd video codec is called out",
      "video is wmv3" in helper.codec_warning({"video": "wmv3", "audio": "aac"}))
check("an odd audio codec is called out",
      "audio is dts" in helper.codec_warning({"video": "h264", "audio": "dts"}))
eq("nothing probed, nothing claimed", helper.codec_warning({}), "")
# Advisory only: support varies by device generation, and guessing wrong must
# not stop someone casting a file that would have worked.
check("the warning says it might still play",
      "will play if" in helper.codec_warning({"video": "wmv3", "audio": "dts"}))

print("=== the file server publishes one file at a time ===")
import tempfile
fs = helper.FileServer()
eq("nothing served yet", fs.serving, 0)
with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
    tmp.write(b"x" * 64)
    sample = tmp.name
try:
    threw = ""
    try:
        fs.url_for("/nonexistent/nope.mp4", "127.0.0.1")
    except Exception as exc:
        threw = str(exc)
    check("a missing file is refused", "not a file" in threw, threw)

    url, real = fs.url_for(sample, "127.0.0.1")
    eq("one token published", fs.serving, 1)
    check("the url carries an opaque token, not the path",
          sample not in url and url.startswith("http://"), url)
    check("the token is long enough to be unguessable",
          len(url.rsplit("/", 1)[1]) >= 16, url)
    eq("the real path is returned separately", real, os.path.realpath(sample))

    fs.release(sample)
    eq("releasing stops serving it", fs.serving, 0)
finally:
    fs.stop()
    os.unlink(sample)

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
