import QtQuick
import Quickshell
import Quickshell.Io
import "Devices.js" as Devices
import "Model.js" as Model

// Everything stateful: the helper process, the device map both surfaces read,
// and the settings that configure them. Declared as kind "service" in the
// manifest so the shell owns exactly one of these. Bar widgets are built once
// per monitor, and a widget-owned helper would open a second TLS connection to
// every device on a two-screen desk.
//
// Nothing here renders. Widget.qml and Panel.qml read `devices` and call
// `send()`; neither of them knows a subprocess exists.
Item {
  id: root
  visible: false

  readonly property string moduleId: "meirdick.cast"
  property var shell: null
  property var settings: ({})

  // ---------------------------------------------------------------- settings
  //
  // The shell injects `settings` from this plugin's shell.json entry, but that
  // injection does not reliably re-run when the file changes underneath it.
  // Watching the file and preferring what it says means a hand edit takes
  // effect on save rather than on the next shell restart. When the injection is
  // working the two agree and this changes nothing.
  readonly property string shellConfigPath: Quickshell.env("HOME") + "/.config/omarchy/shell.json"
  property var fileSettings: ({})

  FileView {
    path: root.shellConfigPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var parsed = root.extractSettings(text())
      if (parsed !== null) root.fileSettings = parsed
    }
    onLoadFailed: root.fileSettings = ({})
  }

  function extractSettings(text) {
    var raw = String(text || "").trim()
    if (raw === "") return null
    var parsed
    try {
      parsed = JSON.parse(raw)
    } catch (e) {
      // A half-written file, caught mid-save by someone else. Keep what we have
      // rather than reverting every setting to its default for a moment.
      return null
    }
    var layout = parsed && parsed.bar && parsed.bar.layout ? parsed.bar.layout : null
    if (layout) {
      var sections = ["left", "center", "right"]
      for (var i = 0; i < sections.length; i++) {
        var entries = layout[sections[i]]
        if (!Array.isArray(entries)) continue
        for (var j = 0; j < entries.length; j++) {
          if (entries[j] && entries[j].id === root.moduleId) return entries[j]
        }
      }
    }
    // Also reachable as a plain service, with no bar entry at all.
    if (Array.isArray(parsed.plugins)) {
      for (var k = 0; k < parsed.plugins.length; k++) {
        var entry = parsed.plugins[k]
        if (entry && entry.id === root.moduleId) return entry
      }
    }
    return ({})
  }

  // Manifest defaults are not merged into the injected settings by the shell,
  // so every default is restated here. Changing one means changing both.
  function setting(name, fallback) {
    var value = fileSettings ? fileSettings[name] : undefined
    if (value === undefined || value === null) value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    if (min !== undefined && n < min) n = min
    if (max !== undefined && n > max) n = max
    return n
  }

  function boolSetting(name, fallback) {
    var value = setting(name, fallback)
    return value === true || value === 1 || value === "true" || value === "1"
  }

  readonly property string barFormat: String(setting("barFormat", "full"))
  readonly property int maxTitle: intSetting("maxTitle", 40, 8, 90)
  readonly property bool showDeviceName: boolSetting("showDeviceName", true)
  readonly property bool hideWhenPaused: boolSetting("hideWhenPaused", false)
  readonly property bool showGlyph: boolSetting("showGlyph", true)
  readonly property string preferredDevice: String(setting("preferredDevice", ""))
  readonly property int volumeStep: intSetting("volumeStep", 5, 1, 25)
  readonly property bool notifyTrack: boolSetting("notifyTrack", false)
  readonly property int helperRestartSec: intSetting("helperRestartSec", 5, 1, 60)
  readonly property string backendList: String(setting("backends", "cast,airplay,androidtv,avahi"))
  readonly property string pythonSetting: String(setting("pythonPath", ""))

  // ------------------------------------------------------------------- state

  property var state: Devices.emptyState()
  property var devices: []
  property int revision: 0

  // These mirror fields on `state`, which is mutated in place rather than
  // replaced — QML re-evaluates a binding when a property changes identity,
  // not when an object it points at is edited, so reading them off `state`
  // directly would leave them stale forever. `devices` has `revision` for the
  // same reason; these are set explicitly in ingest().
  property bool ready: false
  property var missing: ({})
  property var backends: []
  property string helperVersion: ""
  property string lastError: ""
  property bool helperRunning: false
  property int helperFailures: 0

  // The device that owns the bar. Null is the resting state, and the widget
  // renders nothing at all when it is.
  readonly property var barDevice: {
    void revision
    var candidates = root.devices
    if (root.hideWhenPaused) {
      candidates = candidates.filter(function (d) { return d.state !== "PAUSED" })
    }
    return Model.chooseBar(candidates, root.preferredDevice)
  }
  readonly property bool hasMedia: barDevice !== null

  signal deviceUpdated(string id)
  signal trackChanged(string id, string title, string artist)
  signal pairingChanged(string id, string pairState)

  property var _lastTrack: ({})

  function refreshDevices() {
    root.devices = Devices.list(root.state)
    root.revision = root.revision + 1
  }

  // -------------------------------------------------------- the interpreter
  //
  // The backend libraries are optional and none of them are packaged for Arch
  // except pychromecast, so the documented path is a virtualenv the plugin owns.
  // Preferring it when present means a user who followed the README gets every
  // backend, and a user who ran `pacman -S python-pychromecast` still gets Cast.
  readonly property string venvPython:
    Quickshell.env("HOME") + "/.local/share/omarchy/meirdick.cast/venv/bin/python"
  property bool venvExists: false
  readonly property string pythonPath:
    pythonSetting !== "" ? pythonSetting : (venvExists ? venvPython : "python3")

  FileView {
    path: root.venvPython
    printErrors: false
    onLoaded: root.venvExists = true
    onLoadFailed: root.venvExists = false
  }

  readonly property string helperPath: Qt.resolvedUrl("bin/omarchy-cast-helper.py")
    .toString().replace(/^file:\/\//, "")

  // ------------------------------------------------------------- the helper

  Process {
    id: helper
    running: false
    stdinEnabled: true

    // The helper prints one JSON object per line and pushes as devices change,
    // so this is the whole update path. There is no polling anywhere in this
    // plugin: castv2 and AirPlay both broadcast status, and a timer would only
    // add latency to something that already arrives on its own.
    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function (line) { root.ingest(line) }
    }

    stderr: SplitParser {
      splitMarker: "\n"
      onRead: function (line) {
        var text = String(line || "").trim()
        if (text !== "") console.log("[meirdick.cast] " + text)
      }
    }

    onExited: function (exitCode, exitStatus) {
      root.helperRunning = false
      root.helperFailures = root.helperFailures + 1
      console.warn("[meirdick.cast] helper exited (" + exitCode + "), restarting")
      restartTimer.interval = root.backoffMs()
      restartTimer.restart()
    }
  }

  // Backoff, so a helper that cannot start — no python, a broken venv, a
  // syntax error after an edit — does not respawn in a tight loop and fill
  // the log.
  function backoffMs() {
    var base = root.helperRestartSec * 1000
    var scaled = base * Math.pow(2, Math.min(4, root.helperFailures - 1))
    return Math.max(1000, Math.min(60000, scaled))
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: root.startHelper()
  }

  function startHelper() {
    if (helper.running) return
    root.ready = false
    root.backends = []
    var argv = [root.pythonPath, root.helperPath]
    if (root.backendList !== "") argv.push("--only=" + root.backendList)
    helper.command = argv
    helper.running = true
    root.helperRunning = true
  }

  function restartHelper() {
    root.helperFailures = 0
    if (helper.running) {
      helper.running = false      // onExited schedules the restart
    } else {
      root.startHelper()
    }
  }

  // Reconfiguring which backends run means a different command line, so the
  // process has to come back rather than be told.
  onBackendListChanged: if (root.helperRunning) root.restartHelper()
  onPythonPathChanged: if (root.helperRunning) root.restartHelper()

  // -------------------------------------------------------------- ingest

  function ingest(line) {
    var text = String(line || "").trim()
    if (text === "") return

    var before = null
    var parsed = null
    try {
      parsed = JSON.parse(text)
    } catch (e) {
      return
    }
    if (parsed && parsed.type === "device" && parsed.id) {
      var known = Devices.get(root.state, String(parsed.id))
      if (known) before = { title: known.title, artist: known.artist }
    }

    Devices.apply(root.state, text)
    refreshDevices()

    if (!parsed) return
    if (parsed.type === "device") {
      root.helperFailures = 0
      var id = String(parsed.id)
      root.deviceUpdated(id)
      var now = Devices.get(root.state, id)
      if (now && now.title !== "" &&
          (!before || before.title !== now.title || before.artist !== now.artist)) {
        root.trackChanged(id, now.title, now.artist)
        if (root.notifyTrack) root.notifyTrackChange(now)
      }
    } else if (parsed.type === "ready") {
      root.helperFailures = 0
      root.lastError = ""
      root.ready = true
      root.backends = Array.isArray(parsed.backends) ? parsed.backends : []
      root.missing = (parsed.missing && typeof parsed.missing === "object")
        ? parsed.missing : ({})
      root.helperVersion = String(parsed.version || "")
    } else if (parsed.type === "error") {
      root.lastError = String(parsed.message || "")
    } else if (parsed.type === "pairing") {
      root.pairingChanged(String(parsed.id), String(parsed.state))
    }
  }

  function notifyTrackChange(device) {
    Quickshell.execDetached([
      "omarchy-notification-send", "-g", "󰄡",
      device.name, Model.describe(device)
    ])
  }

  // -------------------------------------------------------------- commands

  function send(payload) {
    if (!helper.running) return false
    try {
      helper.write(JSON.stringify(payload) + "\n")
      return true
    } catch (e) {
      console.warn("[meirdick.cast] write failed: " + e)
      return false
    }
  }

  function command(cmd, id) { return send({ cmd: cmd, id: id }) }
  function playPause(id) { return command("playPause", id) }
  function next(id) { return command("next", id) }
  function previous(id) { return command("previous", id) }
  function stop(id) { return command("stop", id) }
  function seek(id, position) { return send({ cmd: "seek", id: id, position: position }) }
  function setVolume(id, level) {
    return send({ cmd: "volume", id: id, level: Math.max(0, Math.min(1, level)) })
  }
  function setMuted(id, muted) { return send({ cmd: "mute", id: id, muted: muted }) }
  function key(id, name) { return send({ cmd: "key", id: id, key: name }) }
  function startPairing(id) { return send({ cmd: "pair", id: id }) }
  function finishPairing(id, code) { return send({ cmd: "pair", id: id, code: String(code) }) }

  function nudgeVolume(id, direction) {
    var device = Devices.get(root.state, id)
    if (!device) return false
    // Android TV has no absolute volume, only steps, so it takes the key path
    // and the slider is approximate there. Saying that here keeps the branch
    // out of both surfaces.
    if (device.kind === "androidtv") {
      return root.key(id, direction > 0 ? "KEYCODE_VOLUME_UP" : "KEYCODE_VOLUME_DOWN")
    }
    if (!(device.volume >= 0)) return false
    return root.setVolume(id, device.volume + direction * (root.volumeStep / 100))
  }

  function choosePreferred(id) {
    // Persisted through the shell so the choice survives a restart. This is a
    // slow round trip via a subprocess, so the binding is not waited on.
    Quickshell.execDetached(["omarchy", "bar", "set", root.moduleId,
                             "preferredDevice", String(id || "")])
  }

  function refresh() { root.send({ cmd: "refresh" }) }

  // What the widget believes, for `omarchy-shell meirdick.cast diagnose`.
  function diagnose() {
    return JSON.stringify({
      helperRunning: root.helperRunning,
      helperFailures: root.helperFailures,
      helperVersion: root.helperVersion,
      python: root.pythonPath,
      venvExists: root.venvExists,
      backendsRequested: root.backendList,
      backendsRunning: root.backends,
      missing: root.missing,
      deviceCount: root.devices.length,
      devices: root.devices.map(function (d) {
        return { id: d.id, name: d.name, kind: d.kind, state: d.state,
                 app: d.app, title: d.title, paired: d.paired }
      }),
      barDevice: root.barDevice ? root.barDevice.id : "",
      lastError: root.lastError
    }, null, 2)
  }

  Component.onCompleted: {
    Model.useDevices(Devices)
    root.startHelper()
  }

  Component.onDestruction: {
    helper.running = false
  }
}
