/**
 * EventDeduper — first-write-wins LRU for control-channel `event_id`s.
 *
 * Under the dual-room architecture the agent fans every `commentary.control`
 * event out to every room and stamps each with a UUID `event_id`. The
 * extension subscribes to data on every room so a dropped packet on one
 * room can still be recovered from another, but downstream handlers
 * must see each logical event exactly once. This is the dedup gate.
 *
 * Implementation is a Map keyed by `event_id` with insertion-order
 * eviction once size exceeds `maxSize`, plus a TTL on each entry. The
 * TTL is generous — pathological cross-room delivery skew arrives within
 * milliseconds; 60s is a memory bound, not a correctness knob.
 */

export const DEDUP_LRU_SIZE = 256;
export const DEDUP_TTL_MS = 60_000;

export class EventDeduper {
  constructor({ maxSize = DEDUP_LRU_SIZE, ttlMs = DEDUP_TTL_MS, now = () => Date.now() } = {}) {
    this._maxSize = maxSize;
    this._ttlMs = ttlMs;
    this._now = now;
    this._cache = new Map();
  }

  // Returns true the first time an `eventId` is seen within the TTL
  // window (caller should dispatch), false on subsequent sightings
  // (caller should drop). Events without an `eventId` pass through
  // unconditionally — non-control payloads predate the fan-out scheme
  // and can't be deduped.
  check(eventId) {
    if (eventId == null) return true;
    const t = this._now();
    const ts = this._cache.get(eventId);
    if (ts !== undefined && t - ts < this._ttlMs) return false;
    if (this._cache.has(eventId)) this._cache.delete(eventId);
    this._cache.set(eventId, t);
    while (this._cache.size > this._maxSize) {
      const oldest = this._cache.keys().next().value;
      if (oldest === undefined) break;
      this._cache.delete(oldest);
    }
    return true;
  }

  reset() {
    this._cache.clear();
  }

  get size() {
    return this._cache.size;
  }
}
