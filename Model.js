// Presentation. Deciding what the bar says, extrapolating the progress bar
// between pushes, and building the panel's rows.
//
// No QML, no I/O, no clock of its own: `now` is a parameter to everything that
// needs it. That is what makes the whole file testable under plain node, and
// it is the rule that keeps a "works until midnight" bug out of the widget.

var Devices = (typeof require !== "undefined") ? require("./Devices.js") : null

// Non-library JS gets a fresh context per QML document, so each QML file that
// imports this hands it the sibling module rather than reaching for require().
function useDevices(mod) { Devices = mod }

var GLYPHS = {
  PLAYING: "▶",
  PAUSED: "⏸",
  BUFFERING: "◌",
  IDLE: "○",
  OFFLINE: "∅",
  UNKNOWN: "○"
}

// -------------------------------------------------------------- extrapolation

// The device reports a position and the instant it was sampled. Between those
// pushes — which arrive only when something actually changes — the panel has
// to move the progress bar itself, or it sits frozen for the length of a song.
function positionAt(device, now) {
  if (!device) return 0
  var base = device.position
  if (device.state !== "PLAYING") return clampPosition(base, device)
  var elapsed = (now - device.at) / 1000
  if (!(elapsed > 0)) return clampPosition(base, device)
  // A stale record — the helper died, the laptop suspended — would otherwise
  // extrapolate hours past the end of the track. Trust it for two minutes.
  if (elapsed > 120) return clampPosition(base, device)
  return clampPosition(base + elapsed * device.rate, device)
}

function clampPosition(value, device) {
  if (!(value > 0)) return 0
  if (device && device.duration > 0 && value > device.duration) return device.duration
  return value
}

function progress(device, now) {
  if (!device || !(device.duration > 0)) return 0
  return Math.max(0, Math.min(1, positionAt(device, now) / device.duration))
}

function formatTime(seconds) {
  var total = Math.floor(Math.max(0, seconds || 0))
  var hours = Math.floor(total / 3600)
  var minutes = Math.floor((total % 3600) / 60)
  var secs = total % 60
  var pad = function (n) { return n < 10 ? "0" + n : String(n) }
  if (hours > 0) return hours + ":" + pad(minutes) + ":" + pad(secs)
  return minutes + ":" + pad(secs)
}

// ------------------------------------------------------------------ ranking

// Which single device gets the bar. Everything else waits in the panel.
//
// Playing beats paused beats buffering, because the bar should describe what
// is audible right now. Within a tier the device that started most recently
// wins: switching from the kitchen to the TV should move the bar to the TV,
// and `at` is the only ordering signal the record carries.
var STATE_RANK = { PLAYING: 3, PAUSED: 2, BUFFERING: 1 }

function score(device) {
  return STATE_RANK[device.state] || 0
}

function chooseBar(devices, preferredId) {
  var candidates = (devices || []).filter(function (d) { return score(d) > 0 })
  if (candidates.length === 0) return null

  if (preferredId) {
    for (var i = 0; i < candidates.length; i++) {
      // A device the user picked in the panel keeps the bar even when
      // something else starts, until it stops being active at all. Having the
      // bar jump away from the thing you just chose is worse than showing the
      // less recent of two players.
      if (candidates[i].id === preferredId) return candidates[i]
    }
  }

  var best = candidates[0]
  for (var j = 1; j < candidates.length; j++) {
    var device = candidates[j]
    if (score(device) > score(best)) best = device
    else if (score(device) === score(best) && device.at > best.at) best = device
  }
  return best
}

// ---------------------------------------------------------------- bar text

function truncate(text, max) {
  var value = String(text || "").replace(/\s+/g, " ").trim()
  if (max <= 0 || value.length <= max) return value
  return value.slice(0, Math.max(0, max - 1)).replace(/[\s,;:.\-]+$/, "") + "…"
}

// What the device is playing, in one phrase. Falls back through the fields the
// backends actually fill: a Cast session may have a title and no artist, an
// Android-TV-native session often has neither and only an app name.
// `withName` is false where the device name is already on screen — repeating
// it as its own subtitle reads as a rendering bug rather than a fallback.
function describe(device, withName) {
  if (!device) return ""
  if (device.title && device.artist) return device.artist + " — " + device.title
  if (device.title) return device.title
  if (device.app) return device.app
  return withName === false ? "" : (device.name || "")
}

// The bar line. Empty string means render nothing at all — the widget is
// silent unless something is casting, so this returning "" is the normal
// resting state, not a failure.
function barText(device, options) {
  if (!device) return ""
  var opts = options || {}
  var format = opts.format || "full"
  if (format === "icon") return ""

  // Wide enough for an ordinary "Artist — Title" without cutting it. The
  // limit borrowed from a title-only widget lands at 28 and truncates
  // "Chris Stapleton — Hard Livin'", which is as plain a track as exists.
  var maxTitle = opts.maxTitle > 0 ? opts.maxTitle : 40
  var what = describe(device)
  if (format === "compact") return truncate(what, maxTitle)

  if (opts.showDevice === false) return truncate(what, maxTitle)
  var deviceName = truncate(device.name, 14)
  if (!what) return deviceName
  return deviceName + ": " + truncate(what, maxTitle)
}

function tooltip(device, devices, now) {
  if (!device) return "No device is playing"
  var lines = []
  var what = describe(device)
  lines.push(device.name + (device.model ? " (" + device.model + ")" : ""))
  if (device.app) lines.push("App: " + device.app)
  if (what && what !== device.app) lines.push(what)
  if (device.album) lines.push("Album: " + device.album)
  lines.push("State: " + device.state.toLowerCase())
  if (device.duration > 0) {
    lines.push(formatTime(positionAt(device, now)) + " / " + formatTime(device.duration))
  }
  if (device.error) lines.push("Error: " + device.error)

  var others = (devices || []).filter(function (d) {
    return d.id !== device.id && Devices && Devices.isActive(d)
  })
  if (others.length === 1) lines.push("1 other device is playing")
  else if (others.length > 1) lines.push(others.length + " other devices are playing")
  return lines.join("\n")
}

function glyph(device) {
  if (!device) return GLYPHS.IDLE
  return GLYPHS[device.state] || GLYPHS.UNKNOWN
}

// ---------------------------------------------------------------- panel rows

// One flat list of heterogeneous rows, the way the other widgets in this house
// do it: the panel renders rows and moves a cursor, and every decision about
// what exists and in what order is made here where it can be tested.
//
// Rows are { kind, id, ... }. Only rows with `selectable` take the cursor.
function buildRows(options) {
  var opts = options || {}
  var devices = opts.devices || []
  var now = opts.now || 0
  var selectedId = opts.selectedId || ""
  var rows = []

  var playing = devices.filter(function (d) { return Devices && Devices.isActive(d) })
  var idle = devices.filter(function (d) { return !(Devices && Devices.isActive(d)) })

  if (playing.length > 0) {
    rows.push({ kind: "header", id: "h-playing", label: "Playing", selectable: false })
    for (var i = 0; i < playing.length; i++) {
      rows.push(deviceRow(playing[i], now, selectedId))
    }
  }

  if (idle.length > 0) {
    rows.push({ kind: "header", id: "h-idle", label: "Idle", selectable: false })
    for (var j = 0; j < idle.length; j++) {
      rows.push(deviceRow(idle[j], now, selectedId))
    }
  }

  if (devices.length === 0) {
    rows.push({
      kind: "empty", id: "empty", selectable: false,
      label: opts.ready ? "No devices found on this network"
                        : "Looking for devices…"
    })
  }

  // Say what is missing, once, at the bottom — a user wondering why their
  // Apple TV is absent should not have to read the README to find out that
  // pyatv is not installed.
  var missing = opts.missing || {}
  var names = Object.keys(missing)
  for (var k = 0; k < names.length; k++) {
    var reason = String(missing[names[k]] || "")
    if (reason.indexOf("disabled") === 0) continue
    rows.push({
      kind: "note", id: "note-" + names[k], selectable: false,
      label: names[k] + ": " + reason
    })
  }

  return rows
}

function deviceRow(device, now, selectedId) {
  return {
    kind: "device",
    id: device.id,
    selectable: true,
    device: device,
    selected: device.id === selectedId,
    label: device.name,
    sublabel: describe(device, false),
    glyph: glyph(device),
    position: positionAt(device, now),
    progress: progress(device, now),
    // A device that is not paired cannot be driven at all, and saying so on
    // the row is the only place the user will look before clicking a button
    // that would do nothing.
    needsPairing: device.paired === false
  }
}

function firstSelectable(rows) {
  for (var i = 0; i < (rows || []).length; i++) {
    if (rows[i].selectable) return i
  }
  return -1
}

function clampCursor(rows, index) {
  var list = rows || []
  if (list.length === 0) return 0
  var bounded = Math.max(0, Math.min(list.length - 1, index))
  if (list[bounded] && list[bounded].selectable) return bounded
  for (var forward = bounded; forward < list.length; forward++) {
    if (list[forward].selectable) return forward
  }
  for (var back = bounded; back >= 0; back--) {
    if (list[back].selectable) return back
  }
  return bounded
}

function moveCursor(rows, index, delta) {
  var list = rows || []
  if (list.length === 0) return 0
  var next = index
  for (var guard = 0; guard < list.length; guard++) {
    next += delta
    if (next < 0 || next >= list.length) return clampCursor(list, index)
    if (list[next].selectable) return next
  }
  return clampCursor(list, index)
}

// How often the widget needs to repaint. Nothing here polls the network — the
// helper pushes — but a running progress bar still has to be redrawn, and
// there is no reason to do that while everything is paused.
function tickIntervalMs(devices) {
  var list = devices || []
  for (var i = 0; i < list.length; i++) {
    if (list[i].state === "PLAYING") return 1000
  }
  return 30000
}

function volumePercent(device) {
  if (!device || !(device.volume >= 0)) return -1
  return Math.round(device.volume * 100)
}

if (typeof module !== "undefined") {
  module.exports = {
    useDevices: useDevices, GLYPHS: GLYPHS, STATE_RANK: STATE_RANK,
    positionAt: positionAt, progress: progress, formatTime: formatTime,
    score: score, chooseBar: chooseBar, truncate: truncate, describe: describe,
    barText: barText, tooltip: tooltip, glyph: glyph,
    buildRows: buildRows, deviceRow: deviceRow,
    firstSelectable: firstSelectable, clampCursor: clampCursor,
    moveCursor: moveCursor, tickIntervalMs: tickIntervalMs,
    volumePercent: volumePercent
  }
}
