import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Devices.js" as Devices
import "Model.js" as Model

// The popout: every device found on the network, what each is playing, and the
// controls for whichever one the cursor is on.
//
// This is a renderer. It owns the cursor and the pairing field and nothing
// else — which rows exist, in what order, and what each says all come from
// Model.buildRows, where they can be tested without a compositor.
Panel {
  id: root
  moduleName: "meirdick.cast"
  ipcTarget: "meirdick.cast"
  manageIpc: false          // a richer IpcHandler is registered below

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  // The popout coordinator identifies panels by their owning bar widget.
  readonly property var barIdentity: hostWidget || root

  // ------------------------------------------------------------------ theme

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color divider: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.09)
  readonly property color panelBackground: Color.popups.background
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color fainter: Qt.darker(foreground, 2.1)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  // ------------------------------------------------------------------ state

  property real nowMs: Date.now()
  property int cursorIndex: 0
  property bool cursorActive: false
  property string pairingFor: ""      // device id awaiting a code, "" when none

  readonly property var devices: service ? service.devices : []
  readonly property var rows: {
    if (service) void service.revision
    return Model.buildRows({
      devices: root.devices,
      now: root.nowMs,
      ready: service ? service.ready : false,
      missing: service ? service.missing : ({}),
      selectedId: service ? service.preferredDevice : ""
    })
  }

  readonly property var currentRow: {
    var list = root.rows
    if (cursorIndex < 0 || cursorIndex >= list.length) return null
    return list[cursorIndex]
  }
  readonly property var currentDevice: currentRow && currentRow.kind === "device"
    ? currentRow.device : null

  // The device the controls act on: whatever the cursor is on, falling back to
  // whatever holds the bar so the panel is useful the instant it opens.
  readonly property var focusDevice: currentDevice
    || (service ? service.barDevice : null)

  Timer {
    interval: service ? Model.tickIntervalMs(root.devices) : 30000
    repeat: true
    running: root.opened
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  onRowsChanged: {
    var bounded = Model.clampCursor(root.rows, root.cursorIndex)
    if (bounded !== root.cursorIndex) root.cursorIndex = bounded
  }

  onOpenedChanged: {
    if (!opened) {
      root.pairingFor = ""
      root.cursorActive = false
      return
    }
    root.nowMs = Date.now()
    root.cursorIndex = Math.max(0, Model.firstSelectable(root.rows))
    if (service) service.refresh()
  }

  Connections {
    target: root.service
    function onPairingChanged(id, pairState) {
      if (pairState === "awaiting-code") {
        root.pairingFor = id
        // The panel's key catcher owns focus, so the field is visible but
        // deaf until focus is moved to it. callLater because the field does
        // not exist yet at the moment `pairingFor` is assigned.
        Qt.callLater(function () { codeField.forceActiveFocus() })
      } else if (pairState === "paired" || pairState === "failed") {
        root.pairingFor = ""
        Qt.callLater(function () { keyCatcher.forceActiveFocus() })
      }
    }
  }

  // -------------------------------------------------------------- behaviour

  function setCursor(index) {
    root.cursorIndex = Model.clampCursor(root.rows, index)
    root.cursorActive = true
  }

  function moveCursor(dx, dy) {
    if (dy === 0) return
    root.cursorIndex = Model.moveCursor(root.rows, root.cursorIndex, dy > 0 ? 1 : -1)
    root.cursorActive = true
  }

  function act(fn) {
    var device = root.focusDevice
    if (!device || !root.service) return
    fn(device)
  }

  function activate() {
    var device = root.focusDevice
    if (!device || !service) return
    // An unpaired Android TV cannot be driven at all, so the obvious action on
    // its row is to start pairing rather than to send a key that will be
    // dropped.
    if (device.needsPairing === true) {
      service.startPairing(device)
      root.pairingFor = device.id
      Qt.callLater(function () { codeField.forceActiveFocus() })
      return
    }
    if (device.can.pause) service.playPause(device.id)
  }

  function submitCode(code) {
    if (root.pairingFor === "" || !service) return
    service.finishPairing(root.pairingFor, code)
  }

  function togglePreferred() {
    var device = root.focusDevice
    if (!device || !service) return
    var next = service.preferredDevice === device.id ? "" : device.id
    service.choosePreferred(next)
  }

  function seekBy(seconds) {
    var device = root.focusDevice
    if (!device || !service || !device.can.seek) return
    var target = Model.positionAt(device, root.nowMs) + seconds
    if (device.duration > 0) target = Math.max(0, Math.min(device.duration, target))
    service.seek(device, Math.max(0, target))
  }

  // -------------------------------------------------------------------- ipc

  IpcHandler {
    target: "meirdick.cast"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { if (root.service) root.service.refresh() }

    function devices(): string {
      if (!root.service) return "[]"
      return JSON.stringify(root.service.devices.map(function (d) {
        return { id: d.id, name: d.name, kind: d.kindLabel,
                 protocols: Object.keys(d.parts), state: d.state,
                 app: d.app, title: d.title, artist: d.artist,
                 volume: d.volume, canVolume: d.can.volume,
                 canVolumeSteps: d.can.volumeSteps,
                 paired: d.paired, needsPairing: d.needsPairing === true }
      }), null, 2)
    }

    function playPause(id: string): string {
      var device = root.resolve(id)
      if (!device) return "no such device"
      root.service.playPause(device)
      return device.id
    }

    function next(id: string): string {
      var device = root.resolve(id)
      if (!device) return "no such device"
      root.service.next(device)
      return device.id
    }

    function previous(id: string): string {
      var device = root.resolve(id)
      if (!device) return "no such device"
      root.service.previous(device)
      return device.id
    }

    function volume(id: string, level: string): string {
      var device = root.resolve(id)
      if (!device) return "no such device"
      root.service.setVolume(device, parseFloat(level))
      return device.id
    }

    function pair(id: string, code: string): string {
      var device = root.resolve(id)
      if (!device) return "no such device"
      // Sending a code is only meaningful inside a session the helper is
      // already holding, so start one first when the caller has not.
      if (String(code) === "") {
        root.service.startPairing(device)
        return "pairing started for " + device.id + " — the code is on the TV"
      }
      root.pairingFor = device.id
      root.service.finishPairing(device, code)
      return "code sent to " + device.id
    }

    function state(): string {
      return JSON.stringify({
        opened: root.opened, cursor: root.cursorIndex,
        focus: root.focusDevice ? root.focusDevice.id : "",
        pairing: root.pairingFor
      })
    }

    function diagnose(): string {
      return root.service ? root.service.diagnose() : "no service"
    }

    function restart(): string {
      if (!root.service) return "no service"
      root.service.restartHelper()
      return "restarting"
    }
  }

  // Accept an exact id, or any unambiguous fragment of a name — typing the
  // full "cast:767a7405…" uuid at a shell prompt is not something anyone will
  // do twice.
  function resolve(id) {
    if (!service) return null
    var wanted = String(id || "").toLowerCase()
    var list = service.devices
    if (wanted === "") return service.barDevice
    for (var i = 0; i < list.length; i++) {
      if (list[i].id.toLowerCase() === wanted) return list[i]
    }
    for (var j = 0; j < list.length; j++) {
      if (list[j].name.toLowerCase().indexOf(wanted) >= 0) return list[j]
    }
    return null
  }

  // ------------------------------------------------------------------ panel

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(
      hero.implicitHeight + list.contentHeight + legend.implicitHeight + Style.space(24),
      Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.pairingFor !== ""

      onMoveRequested: function (dx, dy) {
        if (!root.cursorActive && dy !== 0) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: root.activate()
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onTextKey: function (text) {
        switch (text) {
        case "n": root.act(function (d) { if (d.can.next) root.service.next(d) }); break
        case "b": root.act(function (d) { if (d.can.prev) root.service.previous(d) }); break
        case "s": root.act(function (d) { if (d.can.stop) root.service.stop(d) }); break
        case "m": root.act(function (d) { root.service.setMuted(d, !d.muted) }); break
        case "=":
        case "+": root.act(function (d) { root.service.nudgeVolume(d, 1) }); break
        case "-": root.act(function (d) { root.service.nudgeVolume(d, -1) }); break
        case "f": root.togglePreferred(); break
        case "r": if (root.service) root.service.refresh(); break
        case "R": if (root.service) root.service.restartHelper(); break
        case "[": root.seekBy(-15); break
        case "]": root.seekBy(15); break
        case "g": root.setCursor(Model.firstSelectable(root.rows)); break
        case "G": root.setCursor(root.rows.length - 1); break
        }
      }

      ColumnLayout {
        anchors.fill: parent
        spacing: Style.spacing.rowGap

        // -------------------------------------------------------- now playing

        ColumnLayout {
          id: hero
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          visible: root.focusDevice !== null

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.controlGap

            // Artwork, with the mark drawn underneath it. A Google TV playing
            // through a native app reports no images at all, so the fallback is
            // the common case here and a hole in the layout would be the
            // default appearance rather than an edge case.
            Item {
              implicitWidth: Style.space(64)
              implicitHeight: Style.space(64)

              Rectangle {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
              }

              CastMark {
                anchors.centerIn: parent
                size: Style.space(30)
                color: root.dim
                activeColor: root.accent
                active: true
                playing: !!root.focusDevice && root.focusDevice.state === "PLAYING"
                visible: art.status !== Image.Ready
              }

              Image {
                id: art
                anchors.fill: parent
                source: !!root.focusDevice && root.focusDevice.art !== ""
                  ? root.focusDevice.art : ""
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                // Decode at the size actually drawn rather than whatever the
                // device published; some senders hand over a 1200px cover.
                sourceSize.width: Math.round(Style.space(64) * 2)
                sourceSize.height: Math.round(Style.space(64) * 2)
                visible: status === Image.Ready
              }
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.spacing.hairline

              Text {
                Layout.fillWidth: true
                text: root.focusDevice ? root.focusDevice.name : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
                elide: Text.ElideRight
              }

              Text {
                Layout.fillWidth: true
                text: root.focusDevice ? Model.describe(root.focusDevice, false) : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
                visible: text !== ""
              }

              Text {
                Layout.fillWidth: true
                text: {
                  if (!root.focusDevice) return ""
                  var device = root.focusDevice
                  var bits = [device.kindLabel]
                  if (device.app && device.app !== Model.describe(device, false)) bits.push(device.app)
                  bits.push(device.state.toLowerCase())
                  if (device.detail !== "") bits.push(device.detail)
                  return bits.join(" · ")
                }
                color: root.fainter
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }

          // Seek bar. Only shown when the device says it can seek and knows how
          // long the thing is; a live stream has neither.
          Item {
            Layout.fillWidth: true
            implicitHeight: Style.space(16)
            visible: !!root.focusDevice && root.focusDevice.duration > 0

            Rectangle {
              id: track
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              height: Math.max(2, Style.space(3))
              radius: height / 2
              color: root.divider

              Rectangle {
                width: parent.width * (root.focusDevice
                  ? Model.progress(root.focusDevice, root.nowMs) : 0)
                height: parent.height
                radius: parent.radius
                color: root.accent
              }
            }

            MouseArea {
              anchors.fill: parent
              enabled: !!root.focusDevice && root.focusDevice.can.seek
              cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: function (mouse) {
                if (!root.focusDevice || !root.service) return
                var fraction = Math.max(0, Math.min(1, mouse.x / width))
                root.service.seek(root.focusDevice,
                                  fraction * root.focusDevice.duration)
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true
            visible: !!root.focusDevice && root.focusDevice.duration > 0

            Text {
              text: root.focusDevice
                ? Model.formatTime(Model.positionAt(root.focusDevice, root.nowMs)) : ""
              color: root.fainter
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Item { Layout.fillWidth: true }
            Text {
              text: root.focusDevice ? Model.formatTime(root.focusDevice.duration) : ""
              color: root.fainter
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // Transport. Every button is enabled from what the device said it
          // supports, never from what the protocol allows in general — a
          // YouTube session that will not accept a skip should not offer one.
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.controlGap

            Button {
              text: "󰒮"
              enabled: !!root.focusDevice && root.focusDevice.can.prev
              onClicked: root.act(function (d) { root.service.previous(d) })
            }
            Button {
              text: !!root.focusDevice && root.focusDevice.state === "PLAYING" ? "󰏤" : "󰐊"
              enabled: !!root.focusDevice && root.focusDevice.can.pause
              onClicked: root.act(function (d) { root.service.playPause(d) })
            }
            Button {
              text: "󰒭"
              enabled: !!root.focusDevice && root.focusDevice.can.next
              onClicked: root.act(function (d) { root.service.next(d) })
            }
            Button {
              text: "󰓛"
              enabled: !!root.focusDevice && root.focusDevice.can.stop
              onClicked: root.act(function (d) { root.service.stop(d) })
            }

            Item { Layout.fillWidth: true }

            Text {
              text: !!root.focusDevice && root.focusDevice.volume >= 0
                ? Model.volumePercent(root.focusDevice) + "%" : ""
              color: root.fainter
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // Volume. Hidden entirely when the device owns its own volume, rather
          // than shown as a slider that silently does nothing: a Google TV
          // reports controlType "fixed" and ignores the receiver-channel
          // command, and the honest answer is to say so and point at the remote.
          PanelSlider {
            Layout.fillWidth: true
            visible: !!root.focusDevice && root.focusDevice.can.volume
              && root.focusDevice.volume >= 0
            bar: root.bar
            minimum: 0
            maximum: 1
            step: root.service ? root.service.volumeStep / 100 : 0.05
            value: root.focusDevice ? Math.max(0, root.focusDevice.volume) : 0
            // Only act on a real drag. The bound `value` settling during
            // construction also emits moved(), and obeying that sends a volume
            // command the user never asked for — which on a device that
            // refuses volume surfaces as an error the moment the panel opens.
            onReleased: function (level) {
              if (root.focusDevice && root.service && root.opened) {
                root.service.setVolume(root.focusDevice, level)
              }
            }
          }

          // Step buttons, for a device that has no volume scale to slide along.
          // A Google TV passing audio to a receiver over HDMI-CEC reports
          // max == 0 and accepts only up and down presses.
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.controlGap
            visible: !!root.focusDevice && !root.focusDevice.can.volume
              && root.focusDevice.can.volumeSteps

            Text {
              text: "Volume"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Button {
              text: "󰝞"
              onClicked: root.act(function (d) { root.service.nudgeVolume(d, -1) })
            }
            Button {
              text: "󰝝"
              onClicked: root.act(function (d) { root.service.nudgeVolume(d, 1) })
            }
            Button {
              text: "󰝟"
              onClicked: root.act(function (d) { root.service.setMuted(d, !d.muted) })
            }
            Item { Layout.fillWidth: true }
            Text {
              text: "steps only"
              color: root.fainter
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // Neither a scale nor steps: say why rather than showing nothing.
          Text {
            Layout.fillWidth: true
            visible: !!root.focusDevice && root.focusDevice.volumeFixed
              && !root.focusDevice.can.volume && !root.focusDevice.can.volumeSteps
            text: "Volume is controlled by the TV. Pair the Android TV remote to change it from here."
            color: root.fainter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // Pairing. Only ever reached by an explicit action on an unpaired
          // device — nothing pairs on its own, and the certificate this writes
          // is the only state the plugin keeps outside its own checkout.
          RowLayout {
            Layout.fillWidth: true
            visible: root.pairingFor !== ""
            spacing: Style.spacing.controlGap

            Text {
              text: "Code on TV:"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            TextField {
              id: codeField
              Layout.fillWidth: true
              placeholderText: "6 digits, then enter"
              onAccepted: {
                root.submitCode(text)
                text = ""
              }
              Keys.onEscapePressed: {
                root.pairingFor = ""
                text = ""
                Qt.callLater(function () { keyCatcher.forceActiveFocus() })
              }
            }
          }
        }

        PanelSeparator { Layout.fillWidth: true; visible: hero.visible }

        // ------------------------------------------------------------- rows

        ListView {
          id: list
          Layout.fillWidth: true
          Layout.fillHeight: true
          model: root.rows
          clip: true
          currentIndex: root.cursorIndex
          boundsBehavior: Flickable.StopAtBounds

          delegate: Item {
            required property int index
            required property var modelData

            width: ListView.view.width
            implicitHeight: modelData.kind === "device"
              ? Style.spacing.popupRowHeight
              : Style.space(20)

            Rectangle {
              anchors.fill: parent
              anchors.leftMargin: -Style.spacing.rowPaddingX / 2
              anchors.rightMargin: -Style.spacing.rowPaddingX / 2
              radius: Style.cornerRadius
              visible: modelData.selectable && index === root.cursorIndex && root.cursorActive
              color: root.selectedFill
            }

            // header and note rows
            Text {
              anchors.verticalCenter: parent.verticalCenter
              anchors.left: parent.left
              anchors.right: parent.right
              visible: modelData.kind !== "device"
              text: modelData.label || ""
              color: modelData.kind === "note" ? root.fainter : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.capitalization: modelData.kind === "header"
                ? Font.AllUppercase : Font.MixedCase
              elide: Text.ElideRight
              wrapMode: modelData.kind === "note" ? Text.WordWrap : Text.NoWrap
            }

            RowLayout {
              anchors.fill: parent
              visible: modelData.kind === "device"
              spacing: Style.spacing.controlGap

              CastMark {
                size: Style.space(15)
                color: root.dim
                activeColor: root.accent
                active: true
                playing: !!modelData.device && modelData.device.state === "PLAYING"
                error: !!modelData.device && modelData.device.error !== ""
              }

              ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                Text {
                  Layout.fillWidth: true
                  text: modelData.label || ""
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight
                }

                Text {
                  Layout.fillWidth: true
                  text: modelData.needsPairing
                    ? "not paired — press enter to pair"
                    : (modelData.sublabel || "")
                  color: modelData.needsPairing ? root.urgent : root.fainter
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                  visible: text !== ""
                }
              }

              // The preferred device keeps the bar when several play at once.
              Text {
                text: "󰐾"
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                visible: modelData.selected === true
              }

              Text {
                text: modelData.glyph || ""
                color: !!modelData.device && modelData.device.state === "PLAYING"
                  ? root.accent : root.fainter
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            MouseArea {
              anchors.fill: parent
              enabled: modelData.selectable
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              onClicked: function (mouse) {
                root.setCursor(index)
                if (mouse.button === Qt.RightButton) root.togglePreferred()
                else root.activate()
              }
            }
          }
        }

        // ----------------------------------------------------------- legend

        ColumnLayout {
          id: legend
          Layout.fillWidth: true
          spacing: 0

          PanelSeparator { Layout.fillWidth: true }

          Text {
            Layout.fillWidth: true
            text: "space play · n/b track · [ ] seek · +/- vol · m mute · f prefer · s stop · r refresh"
            color: root.fainter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Text {
            Layout.fillWidth: true
            visible: !!root.service && !root.service.helperRunning
            text: "Helper is not running. Press R to restart it."
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  Component.onCompleted: Model.useDevices(Devices)
}
