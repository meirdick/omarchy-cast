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
    // Some devices have no volume scale at all and can only be nudged up and
    // down — a Google TV passing audio to a receiver over HDMI-CEC, for one.
    // The panel draws step buttons for those instead of a slider.
    volumeSteps: bool(source.volumeSteps),
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

// ------------------------------------------------------------------ merging
//
// One physical device can answer on several protocols, and the user does not
// care. A Google TV Streamer appears twice — once over Cast, which carries the
// track metadata and transport, and once over Android TV Remote, which carries
// volume and the D-pad. Listing both is an implementation detail leaking onto
// the screen, and a harmful one: the row showing the track is not the row that
// can change the volume, so volume looks broken.
//
// They are merged on IP address, which is the only identifier the two
// protocols share. A device with no address is never merged, because guessing
// from a friendly name would eventually fuse two real devices called "TV".

// Which protocol's view of "what is playing" to trust, best first. Android TV
// Remote reports only a package name and never a track, so it always loses.
var MEDIA_PRIORITY = { cast: 3, airplay: 2, androidtv: 1 }

function mergeKey(device) {
  return device.host ? "host:" + device.host : "id:" + device.id
}

function mergedLabel(parts) {
  if (parts.cast && parts.androidtv) return "Google TV"
  var kinds = Object.keys(parts)
  if (kinds.length === 1) return KIND_LABELS[kinds[0]] || kinds[0]
  return kinds.map(function (k) { return KIND_LABELS[k] || k }).join(" + ")
}

function mergeGroup(group) {
  if (group.length === 1) {
    var only = group[0]
    var single = {}
    for (var key in only) single[key] = only[key]
    single.parts = {}
    single.parts[only.kind] = only.id
    single.mediaId = only.id
    single.volumeId = (only.can.volume || only.can.volumeSteps) ? only.id : ""
    single.keyId = only.can.keys ? only.id : ""
    // Set on both paths, so callers never have to ask the question two ways.
    single.needsPairing = only.paired === false
    return single
  }

  var parts = {}
  for (var i = 0; i < group.length; i++) parts[group[i].kind] = group[i].id

  // The base is whichever source has the most authoritative view of playback:
  // one that is actually playing beats one that merely could.
  var base = group[0]
  for (var j = 1; j < group.length; j++) {
    var candidate = group[j]
    var better = ACTIVE[candidate.state] === true && ACTIVE[base.state] !== true
    var samePlayback = (ACTIVE[candidate.state] === true) === (ACTIVE[base.state] === true)
    if (better ||
        (samePlayback &&
         (MEDIA_PRIORITY[candidate.kind] || 0) > (MEDIA_PRIORITY[base.kind] || 0))) {
      base = candidate
    }
  }

  var merged = {}
  for (var field in base) merged[field] = base[field]
  merged.parts = parts
  merged.mediaId = base.id
  merged.kindLabel = mergedLabel(parts)

  // Capabilities are the union, and each one remembers which source provides
  // it, because that is where the command has to be sent.
  merged.can = capabilities({})
  merged.volumeId = ""
  merged.keyId = ""
  var hasScale = false

  for (var k = 0; k < group.length; k++) {
    var part = group[k]
    for (var name in part.can) {
      if (part.can[name]) merged.can[name] = true
    }
    if (part.can.keys && merged.keyId === "") merged.keyId = part.id
    // Prefer a source with a real, settable scale over one that can only
    // step. volumeFixed is the trap here: a Cast receiver attached to a TV
    // reports a readable level and then drops every attempt to change it, so
    // it must not win over an Android TV remote that can actually move the
    // volume, even though stepping is the cruder mechanism.
    if (part.can.volume && part.volume >= 0 && !part.volumeFixed && !hasScale) {
      merged.volumeId = part.id
      merged.volume = part.volume
      merged.muted = part.muted
      merged.volumeFixed = part.volumeFixed
      hasScale = true
    } else if (part.can.volumeSteps && merged.volumeId === "") {
      merged.volumeId = part.id
      merged.volumeFixed = part.volumeFixed
    } else if (part.can.volume && part.volume >= 0 && merged.volume < 0) {
      // Nothing better available: show the level even though setting it will
      // be refused. The panel reads volumeFixed and offers no control.
      merged.volume = part.volume
      merged.muted = part.muted
      merged.volumeFixed = part.volumeFixed
    }
    if (part.paired === false) merged.needsPairing = true
    if (part.error && !merged.error) merged.error = part.error
  }
  if (!hasScale) merged.can.volume = false

  // Merged devices report paired only when every part is. An unpaired part is
  // a capability the user has not unlocked yet, not a broken device.
  merged.paired = group.every(function (d) { return d.paired !== false })
  if (!merged.paired && !merged.detail) merged.detail = "pair for volume and keys"
  return merged
}

function merge(devices) {
  var groups = {}
  var order = []
  for (var i = 0; i < (devices || []).length; i++) {
    var key = mergeKey(devices[i])
    if (!groups[key]) { groups[key] = []; order.push(key) }
    groups[key].push(devices[i])
  }
  return order.map(function (key) { return mergeGroup(groups[key]) })
}

// Where a given command has to be sent for a merged device. Transport and seek
// go to whichever source owns the media session; volume and keys may live on a
// different protocol entirely.
function routeFor(device, cmd) {
  if (!device) return ""
  if (cmd === "volume" || cmd === "mute") return device.volumeId || device.mediaId || device.id
  if (cmd === "key" || cmd === "power") return device.keyId || device.id
  return device.mediaId || device.id
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
  return merge(out)
}

// The unmerged records, for diagnostics.
function raw(state) {
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
    list: list, raw: raw, active: active, isActive: isActive, get: get,
    merge: merge, mergeGroup: mergeGroup, mergeKey: mergeKey,
    routeFor: routeFor, mergedLabel: mergedLabel
  }
}
