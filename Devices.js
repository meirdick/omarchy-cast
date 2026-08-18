// Ingest. Turns the helper's newline-delimited JSON into a device map the
// widget and panel can render, and nothing else.
//
// The boundary this file defends: above it, nobody has heard of Cast, AirPlay
// or Android TV. Every device is one record with the same fields whatever
// spoke it, and a backend that cannot fill a field leaves it empty rather than
// inventing a plausible value. A title that is really "unknown" must read as
// absent, because the bar hides itself on absent and would otherwise show the
// word "unknown" forever.
//
// Every function here is total. The helper is a separate process that can be
// killed, upgraded, or replaced by a half-written line during a partial read,
// so `apply` must survive arbitrary garbage without throwing — an exception
// here takes the whole bar widget down, not just one device.

var STATES = {
  IDLE: true, BUFFERING: true, PLAYING: true,
  PAUSED: true, OFFLINE: true, UNKNOWN: true
}

// A device is "active" when it is worth taking space in the bar for. Buffering
// counts: it is about to play and hiding the widget for those two seconds
// makes the bar jump. Idle and offline do not.
var ACTIVE = { PLAYING: true, PAUSED: true, BUFFERING: true }

var KIND_LABELS = {
  cast: "Cast",
  airplay: "AirPlay",
  androidtv: "Android TV"
}

function str(value) {
  if (value === undefined || value === null) return ""
  if (typeof value === "string") return value
  return String(value)
}

function num(value, fallback) {
  var out = parseFloat(value)
  if (!isFinite(out)) return fallback === undefined ? 0 : fallback
  return out
}

function bool(value) {
  return value === true || value === 1 || value === "true" || value === "1"
}

// The full set of controls, so callers can read `can.seek` without checking
// whether the helper bothered to mention seeking.
function capabilities(raw) {
  var source = (raw && typeof raw === "object") ? raw : {}
  return {
    pause: bool(source.pause),
    seek: bool(source.seek),
    next: bool(source.next),
    prev: bool(source.prev),
    stop: bool(source.stop),
    volume: bool(source.volume),
    mute: bool(source.mute),
    keys: bool(source.keys),
    power: bool(source.power)
  }
}

function normalize(raw) {
  var id = str(raw.id)
  if (id === "") return null
  var state = str(raw.state).toUpperCase()
  if (!STATES[state]) state = "UNKNOWN"

  var kind = str(raw.kind)
  if (kind === "" && id.indexOf(":") > 0) kind = id.split(":")[0]

  return {
    id: id,
    kind: kind,
    kindLabel: KIND_LABELS[kind] || kind,
    name: str(raw.name),
    model: str(raw.model),
    host: str(raw.host),
    app: str(raw.app),
    state: state,
    title: str(raw.title),
    artist: str(raw.artist),
    album: str(raw.album),
    art: str(raw.art),
    position: Math.max(0, num(raw.position)),
    duration: Math.max(0, num(raw.duration)),
    // A zero or negative rate would freeze or reverse the extrapolated
    // progress bar, so it is clamped to something sane here rather than
    // guarded at every use.
    rate: num(raw.rate, 1) > 0 ? num(raw.rate, 1) : 1,
    at: num(raw.at) * 1000,
    volume: num(raw.volume, -1),
    muted: bool(raw.muted),
    volumeFixed: bool(raw.volumeFixed),
    paired: raw.paired === undefined ? true : bool(raw.paired),
    detail: str(raw.detail),
    error: ""
  }
}

// ------------------------------------------------------------------- state

function emptyState() {
  return {
    devices: {},      // id -> device
    order: [],        // ids, in discovery order, so the list does not reshuffle
    ready: false,
    backends: [],
    missing: {},
    version: "",
    pairing: {},      // id -> "awaiting-code" | "paired" | "failed"
    lastError: ""
  }
}

// Apply one parsed message. Returns the same state object, mutated: this runs
// on every line the helper prints, and cloning the whole map each time would
// churn the garbage collector for no benefit.
function apply(state, line) {
  if (!state) state = emptyState()
  var raw
  try {
    raw = JSON.parse(String(line))
  } catch (e) {
    return state
  }
  if (!raw || typeof raw !== "object") return state

  var type = str(raw.type)

  if (type === "ready") {
    state.ready = true
    state.backends = Array.isArray(raw.backends) ? raw.backends.slice() : []
    state.missing = (raw.missing && typeof raw.missing === "object") ? raw.missing : {}
    state.version = str(raw.version)
    return state
  }

  if (type === "device") {
    var device = normalize(raw)
    if (!device) return state
    device.can = capabilities(raw.can)
    var known = state.devices[device.id]
    if (!known) state.order.push(device.id)
    // An OFFLINE record carries no metadata, so keep the last-known name and
    // model rather than replacing a named device with a blank row.
    if (known && device.state === "OFFLINE") {
      device.name = device.name || known.name
      device.model = device.model || known.model
      device.app = device.app || known.app
    }
    state.devices[device.id] = device
    return state
  }

  if (type === "gone") {
    var goneId = str(raw.id)
    delete state.devices[goneId]
    var index = state.order.indexOf(goneId)
    if (index >= 0) state.order.splice(index, 1)
    return state
  }

  if (type === "pairing") {
    state.pairing[str(raw.id)] = str(raw.state)
    return state
  }

  if (type === "error") {
    var message = str(raw.message)
    state.lastError = message
    var target = state.devices[str(raw.id)]
    if (target) target.error = message
    return state
  }

  return state
}

// Devices in a stable order. Discovery order, not alphabetical: a device that
// appears while the panel is open should join the end of the list rather than
// pushing the row under the cursor somewhere else.
function list(state) {
  if (!state) return []
  var out = []
  for (var i = 0; i < state.order.length; i++) {
    var device = state.devices[state.order[i]]
    if (device) out.push(device)
  }
  return out
}

function isActive(device) {
  return !!device && ACTIVE[device.state] === true
}

function active(state) {
  return list(state).filter(isActive)
}

function get(state, id) {
  return (state && state.devices[id]) || null
}

if (typeof module !== "undefined") {
  module.exports = {
    STATES: STATES, ACTIVE: ACTIVE, KIND_LABELS: KIND_LABELS,
    str: str, num: num, bool: bool,
    capabilities: capabilities, normalize: normalize,
    emptyState: emptyState, apply: apply,
    list: list, active: active, isActive: isActive, get: get
  }
}
