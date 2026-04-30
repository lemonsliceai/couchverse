// Run with: node --test src/livekit/event-deduper.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { EventDeduper } from "./event-deduper.js";

test("first sighting of an event_id passes; second is dropped", () => {
  const dedup = new EventDeduper();
  assert.equal(dedup.check("evt-1"), true);
  assert.equal(dedup.check("evt-1"), false);
});

test("two controllers, same event published twice, downstream handler called once", () => {
  // Mimic the SessionLifecycle wiring: a single _onDataReceived shared
  // across two RoomController data subscriptions, gated by one shared
  // EventDeduper. Each "controller" is just a function that invokes
  // the gated handler — exactly the shape the real handler bag has.
  const dedup = new EventDeduper();
  const downstream = [];
  const onDataReceived = (msg) => {
    if (!dedup.check(msg.event_id)) return;
    downstream.push(msg);
  };

  const controllerA = (msg) => onDataReceived(msg);
  const controllerB = (msg) => onDataReceived(msg);

  const event = { type: "commentary_start", event_id: "uuid-abc", speaker: "alien" };
  // The agent fans the same event out to both rooms; the extension
  // subscribes on each. Order between the two arrivals does not
  // matter — first one through wins.
  controllerA(event);
  controllerB(event);

  assert.equal(downstream.length, 1);
  assert.deepEqual(downstream[0], event);
});

test("distinct event_ids both pass through", () => {
  const dedup = new EventDeduper();
  assert.equal(dedup.check("evt-1"), true);
  assert.equal(dedup.check("evt-2"), true);
  assert.equal(dedup.check("evt-1"), false);
  assert.equal(dedup.check("evt-2"), false);
});

test("missing event_id passes through (no dedup possible)", () => {
  const dedup = new EventDeduper();
  // null/undefined event_id can occur on legacy or non-control payloads.
  assert.equal(dedup.check(undefined), true);
  assert.equal(dedup.check(undefined), true);
  assert.equal(dedup.check(null), true);
});

test("LRU eviction caps cache at maxSize", () => {
  const dedup = new EventDeduper({ maxSize: 3 });
  assert.equal(dedup.check("a"), true);
  assert.equal(dedup.check("b"), true);
  assert.equal(dedup.check("c"), true);
  assert.equal(dedup.size, 3);
  // Adding a fourth evicts the oldest ("a").
  assert.equal(dedup.check("d"), true);
  assert.equal(dedup.size, 3);
  // "b", "c", "d" are still cached — none of these mutate insertion order.
  assert.equal(dedup.check("b"), false);
  assert.equal(dedup.check("c"), false);
  assert.equal(dedup.check("d"), false);
  // "a" was evicted, so it reads as fresh again. (Re-caching "a" now
  // evicts "b" — so this assertion has to come last.)
  assert.equal(dedup.check("a"), true);
});

test("entries past TTL are treated as fresh again", () => {
  let now = 1_000;
  const dedup = new EventDeduper({ ttlMs: 60_000, now: () => now });
  assert.equal(dedup.check("evt-1"), true);
  // Advance time past the TTL window.
  now += 60_001;
  assert.equal(dedup.check("evt-1"), true);
  // ...and the freshly-cached copy dedupes again.
  assert.equal(dedup.check("evt-1"), false);
});

test("reset clears the cache", () => {
  const dedup = new EventDeduper();
  dedup.check("evt-1");
  dedup.check("evt-2");
  assert.equal(dedup.size, 2);
  dedup.reset();
  assert.equal(dedup.size, 0);
  assert.equal(dedup.check("evt-1"), true);
});
