// Ingest. Every assertion is about the normalized device record, never about
// the helper's wire format, so the helper can be rewritten as long as the
// record it produces is unchanged.

var fs = require("fs")
var D = require("../Devices.js")

var failures = 0
function check(label, ok, detail) {
  if (!ok) { failures++; console.log("FAIL  " + label + (detail ? "  " + detail : "")) }
}
function eq(label, actual, expected) {
  check(label, actual === expected,
        "got " + JSON.stringify(actual) + " want " + JSON.stringify(expected))
}
function read(name) { return fs.readFileSync(__dirname + "/../fixtures/" + name, "utf8") }

function feed(text) {
  var state = D.emptyState()
  text.split("\n").forEach(function (line) {
    if (line.trim() !== "") D.apply(state, line)
  })
  return state
}

console.log("=== a real session captured from a Google TV Streamer ===")
var live = feed(read("helper-cast-session.ndjson"))
var devices = D.list(live)
eq("one device discovered", devices.length, 1)
var den = devices[0]
eq("friendly name", den.name, "Den TV")
eq("model", den.model, "Google TV Streamer")
eq("kind", den.kind, "cast")
eq("kind label", den.kindLabel, "Cast")
eq("host", den.host, "192.168.2.140")
check("ended up playing after the seek", den.state === "PLAYING", den.state)
eq("title survived the round trip", den.title, "Hard Livin'")
eq("artist", den.artist, "Chris Stapleton")
check("duration parsed", den.duration === 179, String(den.duration))
check("at converted to ms", den.at > 1e12, String(den.at))
// The live capture is the evidence for this: a Google TV wrapping a native app
// reports no artwork at all, so an empty string here is correct behaviour and
// the panel must not treat it as an error.
eq("no artwork, as this device really reports", den.art, "")
eq("volume is fixed on a TV-attached receiver", den.volumeFixed, true)
eq("can pause", den.can.pause, true)
eq("can seek", den.can.seek, true)
eq("cannot send keys over cast", den.can.keys, false)
eq("id is normalized to no dashes", den.id, "cast:767a74054b35365ddef41664cdcc2089")

console.log("=== every state the widget has to render ===")
var mixed = feed(read("helper-states.ndjson"))
eq("ready seen", mixed.ready, true)
eq("backends", mixed.backends.join(","), "cast,androidtv")
check("missing airplay is explained", /pyatv not installed/.test(mixed.missing.airplay),
      JSON.stringify(mixed.missing))
eq("version", mixed.version, "1.0.0")

var all = D.list(mixed)
// Four raw records survive the gone message, but the Google TV answers on both
// Cast and Android TV Remote from one address, so the user sees three devices.
eq("raw records after the gone message", D.raw(mixed).length, 4)
eq("merged into physical devices", all.length, 3)
check("gone id is absent", D.get(mixed, "cast:bbbb2222") === null)
eq("order is discovery order", all.map(function (d) { return d.kind }).join(","),
   "cast,cast,airplay")

var kitchen = D.get(mixed, "cast:aaaa1111")
eq("error attached to its device", kitchen.error, "connection lost")
eq("state-level error recorded", mixed.lastError, "connection lost")
eq("remote artwork url kept", kitchen.art, "https://example.invalid/art.jpg")
eq("volume parsed", kitchen.volume, 0.35)

var atv = D.get(mixed, "androidtv:Android_1AV08283.local")
eq("unpaired device is flagged", atv.paired, false)
eq("and says why", atv.detail, "needs pairing")
eq("pairing state tracked", mixed.pairing["androidtv:Android_1AV08283.local"], "awaiting-code")
eq("kind label for android tv", atv.kindLabel, "Android TV")

console.log("=== merging one physical device ===")
var den = all[0]
eq("both protocols folded into one row",
   Object.keys(den.parts).sort().join("+"), "androidtv+cast")
eq("labelled as what it is", den.kindLabel, "Google TV")
eq("the cast session supplies the metadata", den.title, "Hard Livin'")
// The row showing the track must be the row that changes the volume, or volume
// looks broken to anyone using it.
eq("transport routes to the cast session",
   D.routeFor(den, "seek"), "cast:767a74054b35365ddef41664cdcc2089")
eq("a part id resolves back to the merged device",
   D.mergeKey(den), "host:192.168.2.140")
eq("an unpaired part does not make the whole device unpaired-looking",
   den.needsPairing, true)
eq("and it says what pairing would buy", den.detail, "pair for volume and keys")

// A Cast receiver bolted to a TV reports a readable volume and then drops
// every attempt to set it. It must not win the volume route over a remote that
// can actually move the volume, however crudely.
function part(over) {
  var base = { id: "", kind: "", host: "shared", name: "TV", state: "IDLE",
               volume: -1, muted: false, volumeFixed: false, paired: true,
               detail: "", error: "", title: "", artist: "", album: "", art: "",
               position: 0, duration: 0, rate: 1, at: 0, model: "", app: "",
               kindLabel: "" }
  for (var k in over) base[k] = over[k]
  base.can = D.capabilities(over.can || {})
  return base
}
var fixedCast = part({ id: "cast:f", kind: "cast", volume: 1, volumeFixed: true,
                       state: "PLAYING", title: "x",
                       can: { pause: true, seek: true, volume: true } })
var stepper = part({ id: "atv:f", kind: "androidtv", state: "UNKNOWN",
                     can: { volumeSteps: true, keys: true } })
var pair = D.merge([fixedCast, stepper])[0]
eq("a fixed cast volume does not claim a slider", pair.can.volume, false)
eq("stepping is offered instead", pair.can.volumeSteps, true)
eq("and volume routes to the remote", D.routeFor(pair, "volume"), "atv:f")
eq("while seek still routes to cast", D.routeFor(pair, "seek"), "cast:f")

// A device with no address is never merged: two TVs both called "TV" would
// otherwise fuse into one.
var anonA = part({ id: "cast:a", kind: "cast", host: "" })
var anonB = part({ id: "cast:b", kind: "cast", host: "" })
eq("addressless devices stay separate", D.merge([anonA, anonB]).length, 2)

// needsPairing is answered the same way whether or not anything was merged.
var lone = D.merge([part({ id: "airplay:s", kind: "airplay", paired: false })])[0]
eq("a lone unpaired device says so", lone.needsPairing, true)
eq("a lone paired device does not",
   D.merge([part({ id: "cast:s", kind: "cast" })])[0].needsPairing, false)

console.log("=== active vs idle ===")
var activeIds = D.active(mixed).map(function (d) { return d.id }).sort().join(",")
eq("buffering counts as active, unknown does not",
   activeIds, "airplay:CC11DD22,cast:767a74054b35365ddef41664cdcc2089,cast:aaaa1111")
eq("a paused device is active", D.isActive(D.get(mixed, "cast:aaaa1111")), true)
eq("an unpaired remote on its own is not", D.isActive(atv), false)

console.log("=== garbage in ===")
// The helper is a separate process. It can be killed mid-line, upgraded under
// us, or replaced by something that prints a stack trace. None of that may
// throw, because an exception here takes the bar widget down with it.
var junk = D.emptyState()
var inputs = ["", "   ", "not json at all", "null", "[]", "42", '"a string"',
              '{"type":"device"}', '{"type":"device","id":""}',
              '{"type":"unknown","id":"x"}', '{"type":"gone"}',
              '{"type":"device","id":"cast:z","position":"nonsense","rate":0,"at":null}']
var threw = ""
inputs.forEach(function (line) {
  try { D.apply(junk, line) } catch (e) { threw = String(e) }
})
eq("nothing thrown", threw, "")
var z = D.get(junk, "cast:z")
check("the one valid-ish record landed", z !== null)
eq("nonsense position became zero", z.position, 0)
eq("zero rate was clamped to one", z.rate, 1)
eq("kind inferred from the id prefix", z.kind, "cast")
eq("unknown state defaulted", z.state, "UNKNOWN")

console.log("=== offline keeps what it knew ===")
var off = D.emptyState()
D.apply(off, '{"type":"device","id":"cast:q","name":"Den TV","model":"Streamer","app":"YouTube","state":"PLAYING"}')
D.apply(off, '{"type":"device","id":"cast:q","state":"OFFLINE"}')
var gone = D.get(off, "cast:q")
eq("name retained through an offline record", gone.name, "Den TV")
eq("model retained", gone.model, "Streamer")
eq("state is offline", gone.state, "OFFLINE")

console.log(failures === 0 ? "\nOK" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)
