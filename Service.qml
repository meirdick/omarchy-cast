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
  readonly property bool allowFileServing: boolSetting("allowFileServing", true)
  readonly property int fileServerPort: intSetting("fileServerPort", 8927, 1024, 65535)

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
  property string lastWarning: ""
  property bool helperRunning: false
  property int helperFailures: 0

  // The file currently published over HTTP, if any. Worth surfacing: a socket
  // is open on the LAN for as long as this is set, and the user should be able
  // to see that without reading a log.
  property string servingPath: ""
  property string servingTitle: ""

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

  // Which interpreter to use is decided by sh at launch, not by QML.
  //
  // Testing for the venv with a FileView meant pointing a file reader at a
  // five-megabyte executable, and its loaded/failed result flipped
  // `pythonPath` back and forth. Every flip restarted the helper, which threw
  // away any pairing session in progress — the code you typed went to a
  // process that no longer existed.
  readonly property string pythonPath: pythonSetting

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

    // $1 an explicit interpreter, $2 the plugin's venv, $3 the helper, then
    // its flags. Each flag stays its own argument — collapsing them into one
    // string would hand python a single unparseable argv entry.
    var script =
      'if [ -n "$1" ]; then exec "$1" "$3" "$4" "$5"; ' +
      'elif [ -x "$2" ]; then exec "$2" "$3" "$4" "$5"; ' +
      'else exec python3 "$3" "$4" "$5"; fi'
    var only = root.backendList !== ""
      ? "--only=" + root.backendList : "--only=cast,airplay,androidtv,avahi"
    helper.command = ["sh", "-c", script, "sh",
                      root.pythonSetting, root.venvPython, root.helperPath,
                      only, "--port=" + root.fileServerPort]
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
      if (now && root.servingPath !== "" &&
          (now.state === "IDLE" || now.state === "OFFLINE")) {
        // Playback ended, so the socket has no reason to stay open.
        root.send({ cmd: "stopCast", id: id, path: root.servingPath })
        root.servingPath = ""
        root.servingTitle = ""
      }
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
    } else if (parsed.type === "casting") {
      root.servingPath = String(parsed.path || "")
      root.servingTitle = String(parsed.title || "")
      root.lastWarning = ""
    } else if (parsed.type === "warning") {
      root.lastWarning = String(parsed.message || "")
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

  // One physical device can answer on several protocols, so a command has to
  // go to the one that can actually carry it: seek to the Cast session, volume
  // to the Android TV remote. Callers pass the merged device and never think
  // about it. An id is still accepted, for IPC.
  function route(target, cmd) {
    if (!target) return ""
    if (typeof target === "string") {
      var known = root.find(target)
      return known ? Devices.routeFor(known, cmd) : target
    }
    return Devices.routeFor(target, cmd)
  }

  // Find a merged device by any of its part ids, or its own.
  function find(id) {
    var wanted = String(id || "")
    for (var i = 0; i < root.devices.length; i++) {
      var device = root.devices[i]
      if (device.id === wanted) return device
      for (var kind in device.parts) {
        if (device.parts[kind] === wanted) return device
      }
    }
    return null
  }

  function command(cmd, target) { return send({ cmd: cmd, id: root.route(target, cmd) }) }
  function playPause(target) { return command("playPause", target) }
  function next(target) { return command("next", target) }
  function previous(target) { return command("previous", target) }
  function stop(target) { return command("stop", target) }
  function seek(target, position) {
    return send({ cmd: "seek", id: root.route(target, "seek"), position: position })
  }
  function setVolume(target, level) {
    return send({ cmd: "volume", id: root.route(target, "volume"),
                  level: Math.max(0, Math.min(1, level)) })
  }
  function setMuted(target, muted) {
    return send({ cmd: "mute", id: root.route(target, "mute"), muted: muted })
  }
  function key(target, name) {
    return send({ cmd: "key", id: root.route(target, "key"), key: String(name) })
  }
  function startPairing(target) {
    return send({ cmd: "pair", id: root.route(target, "key") })
  }
  function finishPairing(target, code) {
    return send({ cmd: "pair", id: root.route(target, "key"), code: String(code) })
  }

  function nudgeVolume(target, direction) {
    var device = (typeof target === "string") ? root.find(target) : target
    if (!device) return false
    // A device with no volume scale — a Google TV passing audio to a receiver
    // over HDMI-CEC — can only be nudged, so it takes the key path. Deciding
    // that here keeps the branch out of both surfaces.
    if (!device.can.volume && device.can.volumeSteps) {
      return root.key(device, direction > 0 ? "KEYCODE_VOLUME_UP" : "KEYCODE_VOLUME_DOWN")
    }
    if (!(device.volume >= 0)) return false
    return root.setVolume(device, device.volume + direction * (root.volumeStep / 100))
  }

  function choosePreferred(id) {
    // Persisted through the shell so the choice survives a restart. This is a
    // slow round trip via a subprocess, so the binding is not waited on.
    Quickshell.execDetached(["omarchy", "bar", "set", root.moduleId,
                             "preferredDevice", String(id || "")])
  }

  // Casting a local file. The helper publishes it over HTTP on the LAN under a
  // one-off token and hands the device the URL; nothing is transcoded, so a
  // file the receiver cannot decode will say so rather than play silently.
  function castFile(target, path, title) {
    if (String(path || "") === "") return false
    if (!root.allowFileServing) {
      root.lastError = "casting local files is turned off in settings"
      return false
    }
    return send({ cmd: "castFile", id: root.route(target, "seek"),
                  path: String(path), title: String(title || "") })
  }

  function stopCast(target) {
    var path = root.servingPath
    root.servingPath = ""
    root.servingTitle = ""
    return send({ cmd: "stopCast", id: root.route(target, "seek"), path: path })
  }

  function refresh() { root.send({ cmd: "refresh" }) }

  // What the widget believes, for `omarchy-shell meirdick.cast diagnose`.
  function diagnose() {
    return JSON.stringify({
      helperRunning: root.helperRunning,
      helperFailures: root.helperFailures,
      helperVersion: root.helperVersion,
      pythonOverride: root.pythonSetting,
      venvPython: root.venvPython,
      backendsRequested: root.backendList,
      backendsRunning: root.backends,
      missing: root.missing,
      deviceCount: root.devices.length,
      devices: root.devices.map(function (d) {
        return { id: d.id, name: d.name, kind: d.kindLabel, state: d.state,
                 app: d.app, title: d.title, paired: d.paired,
                 parts: d.parts, volumeId: d.volumeId, keyId: d.keyId,
                 canVolume: d.can.volume, canSteps: d.can.volumeSteps }
      }),
      rawDevices: Devices.raw(root.state).map(function (d) {
        return { id: d.id, kind: d.kind, host: d.host, state: d.state,
                 paired: d.paired }
      }),
      barDevice: root.barDevice ? root.barDevice.id : "",
      servingPath: root.servingPath,
      lastError: root.lastError,
      lastWarning: root.lastWarning
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
