import QtQuick
import qs.Commons

// The plugin's own icon: a screen with signal arcs, the shape every cast
// button has used for a decade.
//
// Drawn rather than set in a Nerd Font, because the shell's font family is the
// fontconfig alias "monospace", which Qt does not reliably resolve to the
// concrete Nerd Font. A private-use codepoint then renders as whatever
// fallback happens to own it.
//
// State is carried by the mark itself so the bar does not need a second
// element to say what is happening:
//
//   idle       outline only, dim          — nothing is casting
//   connected  outline plus one arc       — a device is there, not playing
//   playing    outline plus two arcs      — breathing, in the theme accent
//
// Playing is not an alarm. It gets the accent; urgent stays reserved for the
// states that mean something is wrong.
Item {
  id: root

  property color color: Color.foreground
  property color activeColor: Color.accent
  property bool active: false        // a device is present
  property bool playing: false
  property bool error: false
  property real size: Style.bar.iconCanvas

  implicitWidth: size
  implicitHeight: size

  readonly property color drawColor: error ? Color.urgent
                                   : (playing ? activeColor : color)
  readonly property real unit: Math.max(1, Math.round(size / 16))

  // The screen. Three sides plus a base, so the bottom-left corner stays open
  // for the arcs the way the standard glyph does.
  Item {
    id: screen
    width: root.size * 0.82
    height: root.size * 0.62
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.top: parent.top
    anchors.topMargin: Math.round(root.size * 0.1)

    Rectangle {   // top
      width: parent.width; height: root.unit
      color: root.drawColor
      opacity: root.active ? 0.95 : 0.55
      radius: root.unit / 2
    }
    Rectangle {   // right
      x: parent.width - root.unit; width: root.unit; height: parent.height
      color: root.drawColor
      opacity: root.active ? 0.95 : 0.55
      radius: root.unit / 2
    }
    Rectangle {   // left, stopping short so the arcs have room
      width: root.unit; height: parent.height * 0.42
      color: root.drawColor
      opacity: root.active ? 0.95 : 0.55
      radius: root.unit / 2
    }
    Rectangle {   // base, from the right back toward the arcs
      y: parent.height - root.unit
      x: parent.width * 0.42
      width: parent.width * 0.58; height: root.unit
      color: root.drawColor
      opacity: root.active ? 0.95 : 0.55
      radius: root.unit / 2
    }
  }

  // Signal arcs, drawn as quarter-ring segments from the bottom-left corner.
  Item {
    id: arcs
    anchors.left: screen.left
    anchors.bottom: screen.bottom
    width: root.size * 0.5
    height: root.size * 0.5
    // Each ring is a full circle centred on this item's bottom-left corner;
    // clipping to the item's bounds is what leaves the quarter arc. Without
    // this the whole circle draws and the mark reads as a spiral.
    clip: true

    Repeater {
      model: 2
      delegate: Rectangle {
        required property int index
        // Two nested quarter rings. A Rectangle with a radius equal to its
        // width is a circle; clipping it to the corner leaves the arc, and
        // that avoids pulling in a Canvas or a Shape for eight pixels.
        readonly property real ring: root.size * (0.22 + index * 0.17)
        width: ring * 2
        height: ring * 2
        x: -ring
        y: arcs.height - ring
        radius: ring
        color: "transparent"
        border.width: root.unit
        border.color: root.drawColor
        visible: root.playing || (root.active && index === 0)
        opacity: root.playing ? 1.0 : 0.75

        SequentialAnimation on opacity {
          running: root.playing
          loops: Animation.Infinite
          NumberAnimation { to: 0.35; duration: 1100; easing.type: Easing.InOutQuad }
          NumberAnimation { to: 1.0; duration: 1100; easing.type: Easing.InOutQuad }
        }
      }
    }
  }

  // The dot at the corner the arcs radiate from. Always present when a device
  // is connected, so the mark never reads as empty while a device is idle.
  Rectangle {
    width: root.unit * 2
    height: root.unit * 2
    radius: width / 2
    anchors.left: screen.left
    anchors.bottom: screen.bottom
    color: root.drawColor
    visible: root.active
    opacity: root.playing ? 1.0 : 0.8
  }

  clip: true
}
