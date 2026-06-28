import test from "node:test";
import assert from "node:assert/strict";
import {
  CHANNEL_PREFIX,
  INVOKE_CHANNELS,
  SEND_CHANNELS,
  EVENT_CHANNELS,
  isInvokeChannel,
  isSendChannel,
  isEventChannel,
  KINORA_PROTOCOL,
} from "../../dist-electron/shared/ipc-contract.js";

test("every channel uses the kinora: prefix", () => {
  for (const c of [...INVOKE_CHANNELS, ...SEND_CHANNELS, ...EVENT_CHANNELS]) {
    assert.ok(c.startsWith(CHANNEL_PREFIX), `${c} missing prefix`);
  }
});

test("the three allowlists are disjoint", () => {
  const all = [...INVOKE_CHANNELS, ...SEND_CHANNELS, ...EVENT_CHANNELS];
  assert.equal(new Set(all).size, all.length, "duplicate channel across lists");
});

test("allowlists are frozen", () => {
  assert.equal(Object.isFrozen(INVOKE_CHANNELS), true);
  assert.equal(Object.isFrozen(SEND_CHANNELS), true);
  assert.equal(Object.isFrozen(EVENT_CHANNELS), true);
});

test("type-guards match their lists and reject foreign names", () => {
  assert.equal(isInvokeChannel("kinora:pick-book"), true);
  assert.equal(isInvokeChannel("kinora:renderer-ready"), false); // a send channel
  assert.equal(isInvokeChannel("kinora:bogus"), false);
  assert.equal(isInvokeChannel(42), false);

  assert.equal(isSendChannel("kinora:renderer-ready"), true);
  assert.equal(isSendChannel("kinora:pick-book"), false);

  assert.equal(isEventChannel("kinora:add-book"), true);
  assert.equal(isEventChannel("kinora:pick-book"), false);
});

test("protocol scheme is kinora", () => {
  assert.equal(KINORA_PROTOCOL, "kinora");
});
