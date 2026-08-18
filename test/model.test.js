// Presentation. Every time-dependent assertion is against a fixed instant, and
// `now` is always passed in rather than read, so this suite says the same thing
// whenever it runs.

var fs = require("fs")
var D = require("../Devices.js")
var M = require("../Model.js")
M.useDevices(D)

var failures = 0
function check(label, ok, detail) {
  if (!ok) { failures++; console.log("FAIL  " + label + (detail ? "  " + detail : "")) }
}
function eq(label, actual, expected) {
  check(label, actual === expected,
        "got " + JSON.stringify(actual) + " want " + JSON.stringify(expected))
}
function read(name) { return fs.readFileSync(__dirname + "/../fixtures/" + name, "utf8") }

// A fixed instant so nothing here depends on when the suite runs.
var NOW = new Date(2026, 7, 18, 21, 30, 0).getTime()

function device(over) {
  var base = {
    id: "cast:x", kind: "cast", kindLabel: "Cast", name: "Den TV", model: "",
    host: "", app: "", state: "PLAYING", title: "", artist: "", album: "",
    art: "", position: 30, duration: 180, rate: 1, at: NOW, volume: 0.5,
    muted: false, volumeFixed: false, paired: true, detail: "", error: "",
    can: D.capabilities({})
  }
  for (var key in (over || {})) base[key] = over[key]
  return base
}

console.log("=== the progress bar moves between pushes ===")
// The device only pushes when something changes, so a playing track's position
// has to be extrapolated or the bar sits frozen for three minutes.
eq("at the sampled instant", M.positionAt(device(), NOW), 30)
eq("ten seconds later", M.positionAt(device(), NOW + 10000), 40)
eq("paused does not advance", M.positionAt(device({ state: "PAUSED" }), NOW + 10000), 30)
eq("double rate", M.positionAt(device({ rate: 2 }), NOW + 10000), 50)
eq("never past the end", M.positionAt(device({ position: 179 }), NOW + 60000), 180)
// A suspended laptop or a dead helper leaves a stale record behind. Running
// the clock forward from it would show a track finishing that never played.
eq("a stale record stops extrapolating", M.positionAt(device(), NOW + 600000), 30)
eq("progress fraction", Math.round(M.progress(device(), NOW + 60000) * 100) / 100, 0.5)
eq("no duration means no progress", M.progress(device({ duration: 0 }), NOW), 0)

console.log("=== time formatting ===")
eq("under a minute", M.formatTime(9), "0:09")
eq("minutes", M.formatTime(95), "1:35")
eq("exactly an hour", M.formatTime(3600), "1:00:00")
eq("hours and change", M.formatTime(3725), "1:02:05")
eq("negative clamps", M.formatTime(-5), "0:00")
eq("undefined clamps", M.formatTime(undefined), "0:00")

console.log("=== which device gets the bar ===")
var playing = device({ id: "a", state: "PLAYING", at: NOW - 5000 })
var paused = device({ id: "b", state: "PAUSED", at: NOW })
var buffering = device({ id: "c", state: "BUFFERING", at: NOW })
var idle = device({ id: "d", state: "IDLE", at: NOW })

eq("nothing playing means nothing in the bar", M.chooseBar([idle], ""), null)
eq("empty list", M.chooseBar([], ""), null)
eq("playing beats paused even when older", M.chooseBar([paused, playing], "").id, "a")
eq("paused beats buffering", M.chooseBar([buffering, paused], "").id, "b")
eq("newest wins within a tier",
   M.chooseBar([device({ id: "old", at: NOW - 9000 }), device({ id: "new", at: NOW })], "").id,
   "new")
// Choosing a device in the panel should stick. Having the bar jump to whatever
// started most recently would undo the choice the user just made.
eq("an explicit choice holds the bar", M.chooseBar([playing, paused], "b").id, "b")
eq("but only while it is still active", M.chooseBar([playing, idle], "d").id, "a")

console.log("=== bar text ===")
var full = device({ name: "Den TV", title: "Hard Livin'", artist: "Chris Stapleton" })
eq("artist and title", M.barText(full, {}), "Den TV: Chris Stapleton — Hard Livin'")
eq("compact drops the device", M.barText(full, { format: "compact" }),
   "Chris Stapleton — Hard Livin'")
eq("icon format is silent", M.barText(full, { format: "icon" }), "")
eq("nothing playing is the empty string", M.barText(null, {}), "")
// This is the fallback the Google TV actually needs: a native app session with
// no track metadata at all, only the app name.
eq("app name when there is no track",
   M.barText(device({ name: "Den TV", app: "YouTube" }), {}), "Den TV: YouTube")
eq("title alone", M.describe(device({ title: "The Bear" })), "The Bear")
eq("device name is the last resort", M.describe(device({ name: "Kitchen" })), "Kitchen")
// The hero already shows the name above the subtitle; repeating it there reads
// as a rendering bug rather than a fallback.
eq("but not where the name is already on screen",
   M.describe(device({ name: "Kitchen" }), false), "")
eq("a real title still shows with withName false",
   M.describe(device({ name: "Kitchen", title: "Weightless" }), false), "Weightless")
eq("the default fits an ordinary artist and title",
   M.barText(device({ name: "TV", title: "Hard Livin'", artist: "Chris Stapleton" }), {}),
   "TV: Chris Stapleton — Hard Livin'")
check("long titles are cut",
      M.barText(device({ name: "TV", title: "x".repeat(80) }), { maxTitle: 10 }).length < 24)
eq("truncation ends in an ellipsis", M.truncate("abcdefghij", 5), "abcd…")
eq("short text is untouched", M.truncate("abc", 10), "abc")

console.log("=== tooltip ===")
var tip = M.tooltip(device({ app: "YouTube Music", title: "Hard Livin'",
                             artist: "Chris Stapleton", album: "Traveller",
                             model: "Google TV Streamer" }), [], NOW)
check("names the device and model", /Den TV \(Google TV Streamer\)/.test(tip), tip)
check("names the app", /App: YouTube Music/.test(tip), tip)
check("shows elapsed over total", /0:30 \/ 3:00/.test(tip), tip)
eq("no device", M.tooltip(null, [], NOW), "No device is playing")
var many = M.tooltip(playing, [playing, paused, buffering], NOW)
check("counts the others", /2 other devices are playing/.test(many), many)

console.log("=== panel rows ===")
var state = D.emptyState()
read("helper-states.ndjson").split("\n").forEach(function (l) {
  if (l.trim()) D.apply(state, l)
})
var rows = M.buildRows({
  devices: D.list(state), now: NOW, ready: true,
  missing: state.missing, selectedId: "cast:aaaa1111"
})
var kinds = rows.map(function (r) { return r.kind }).join(",")
eq("headers, devices, then the note",
   kinds, "header,device,device,device,header,device,note")
eq("playing section first", rows[0].label, "Playing")
eq("three active devices", rows.filter(function (r) {
  return r.kind === "device" && r.selectable
}).length, 4)
eq("the selected row knows it", rows.filter(function (r) {
  return r.selected
}).length, 1)
check("the missing backend is explained to the user",
      /pyatv not installed/.test(rows[rows.length - 1].label), rows[rows.length - 1].label)
var unpaired = rows.filter(function (r) { return r.needsPairing })
eq("the unpaired remote is flagged on its row", unpaired.length, 1)

var emptyRows = M.buildRows({ devices: [], now: NOW, ready: true })
eq("says so when there is nothing", emptyRows[0].label, "No devices found on this network")
eq("and says something else while still looking",
   M.buildRows({ devices: [], now: NOW, ready: false })[0].label, "Looking for devices…")

console.log("=== cursor ===")
eq("first selectable skips the header", M.firstSelectable(rows), 1)
eq("cursor lands on a selectable row", M.clampCursor(rows, 0), 1)
eq("moving down skips the second header", M.moveCursor(rows, 3, 1), 5)
eq("moving up skips it too", M.moveCursor(rows, 5, -1), 3)
eq("cannot walk off the end", M.moveCursor(rows, 5, 1), 5)
eq("cannot walk off the start", M.moveCursor(rows, 1, -1), 1)
eq("empty rows are survivable", M.moveCursor([], 0, 1), 0)

console.log("=== repaint rate ===")
eq("a playing device needs a second hand", M.tickIntervalMs([playing]), 1000)
eq("nothing playing can idle", M.tickIntervalMs([paused, idle]), 30000)
eq("no devices at all", M.tickIntervalMs([]), 30000)

console.log("=== volume ===")
eq("as a percentage", M.volumePercent(device({ volume: 0.35 })), 35)
eq("a device with no volume reports -1", M.volumePercent(device({ volume: -1 })), -1)
eq("no device", M.volumePercent(null), -1)

console.log(failures === 0 ? "\nOK" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)
