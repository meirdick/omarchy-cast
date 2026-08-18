import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Devices.js" as Devices
import "Model.js" as Model

// The bar slot. Renders nothing at all when no device is playing, which is
// most of the time — that is the point of the widget, not a degraded state.
//
// This owns no state and runs no processes. The helper lives in Service.qml,
// which the shell instantiates once as a plugin service; this reaches it
// through the shell rather than building its own, because bar widgets are
// created per monitor and two of these must not mean two helpers.
BarWidget {
  id: root
  moduleName: "meirdick.cast"

  readonly property var service: bar && bar.shell
    ? bar.shell.serviceFor("meirdick.cast") : null

  readonly property var device: service ? service.barDevice : null
  readonly property bool hasMedia: device !== null

  // Repaint only as fast as something is actually moving. Nothing here polls
  // the network; this is purely so the elapsed time in the tooltip and the
  // panel's progress bar advance between the helper's pushes.
  property real nowMs: Date.now()
  Timer {
    interval: root.service ? Model.tickIntervalMs(root.service.devices) : 30000
    repeat: true
    running: root.hasMedia
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  readonly property string barText: {
    if (!device || !service) return ""
    void service.revision
    return Model.barText(device, {
      format: service.barFormat,
      maxTitle: service.maxTitle,
      showDevice: service.showDeviceName
    })
  }

  readonly property string glyph: {
    if (!device || !service || !service.showGlyph) return ""
    return Model.glyph(device)
  }

  readonly property string label: {
    if (barText === "") return ""
    return glyph === "" ? barText : glyph + " " + barText
  }

  readonly property string tooltip: {
    if (!service) return "Cast"
    void service.revision
    if (!device) {
      var count = service.devices.length
      if (!service.ready) return "Cast — looking for devices"
      if (count === 0) return "Cast — no devices found"
      return "Cast — " + count + (count === 1 ? " device, idle" : " devices, idle")
    }
    return Model.tooltip(device, service.devices, root.nowMs)
  }

  readonly property bool hasError: service ? service.lastError !== "" : false

  // Shape contract for shell.summon/hide/toggle routing: Bar.findPanelWidget
  // requires open/close/opened on the bar-widget root, not on the nested panel.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }
  function refresh() { if (service) service.refresh() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = root.service
  }
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  onServiceChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.label
    tooltipText: root.tooltip
    // The widget genuinely takes no space when nothing is casting. A device
    // that is merely present does not earn a slot; something has to be playing.
    hasVisualContent: root.hasMedia
    labelVisible: root.label !== ""
    visible: root.hasMedia
    active: root.hasError
    activeColor: root.bar ? root.bar.urgent : Color.urgent
    textRotation: root.vertical ? 90 : 0

    onPressed: function (code) {
      if (code === Qt.RightButton) root.refresh()
      else if (code === Qt.MiddleButton) {
        if (root.service && root.device) root.service.playPause(root.device.id)
      } else root.toggle()
    }

    onWheelMoved: function (delta) {
      if (!root.service || !root.device) return
      root.service.nudgeVolume(root.device.id, delta > 0 ? 1 : -1)
    }
  }

  Component.onCompleted: {
    // Non-library JS gets one context per QML document, so each file that
    // imports Model hands it the sibling module itself.
    Model.useDevices(Devices)
  }
}
