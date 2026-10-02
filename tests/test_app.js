const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function browser(now) {
  const elements = new Map();
  const timers = new Map();
  let timerId = 0;
  class Clock extends Date {
    static now() { return now.value; }
  }
  const context = vm.createContext({
    Date: Clock, Intl,
    fetch: () => new Promise(() => {}),
    clearTimeout: id => timers.delete(id),
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, {
          innerHTML: "", textContent: "", value: "",
          addEventListener() {}, querySelectorAll: () => [],
        });
        return elements.get(selector);
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app.js"), "utf8"), context);
  return { context, elements, timers };
}

function payload(now, { startMinutes = 11, priceAge = 0 } = {}) {
  const updated = new Date(now - priceAge * 60000).toISOString();
  const metrics = (team, probability, odds) => ({ team, modelProbability: probability,
    marketProbability: probability - 5, edgePp: 5, expectedReturnPct: 24,
    odds, bookmaker: "Test", lastUpdate: updated });
  return {
    updatedAt: new Date(now).toISOString(), source: "Test", methodVersion: "test", hitters: {},
    lineupStatus: "projected", valueBetStatus: "connected",
    games: [{ time: "18:30", park: "잠실", away: "LG", home: "두산", pick: "두산",
      awayProb: 54, homeProb: 46, confidence: "접전", reasons: [],
      valueBet: { available: true, recommendation: true, bookmakerCount: 2,
        lastUpdate: updated, commenceTime: new Date(now + startMinutes * 60000).toISOString(),
        favorite: metrics("LG", 54, 1.55), underdog: metrics("두산", 46, 2.7), returnAdvantagePp: 20,
        quality: { marketFresh: true, bettingOpen: true, enoughBookmakers: true },
        criterion: { maximumAgeMinutes: 60, closeBeforeMinutes: 10, minimumBookmakers: 2,
          minimumEvPct: 8, minimumEdgePp: 5, minimumAdvantagePp: 10 },
      },
    }],
  };
}

test("an open page removes its recommendation exactly at the cutoff without another request", () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements, timers } = browser(now);
  context.payload = payload(now.value);
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.match(elements.get("#valueContent").innerHTML, /역배 PICK/);
  const timer = [...timers.values()][0];
  assert.equal(timer.delay, 60000);
  now.value += 60000;
  timer.callback();
  assert.doesNotMatch(elements.get("#valueContent").innerHTML, /역배 PICK/);
  assert.match(elements.get("#valueContent").innerHTML, /NO BET/);
  assert.doesNotMatch(elements.get("#gameGrid").innerHTML, /역배 EV\+/);
});

test("price freshness expiration removes a recommendation on the open page", () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements, timers } = browser(now);
  context.payload = payload(now.value, { startMinutes: 120, priceAge: 59 });
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  const timer = [...timers.values()][0];
  assert.equal(timer.delay, 60001);
  now.value += 60001;
  timer.callback();
  assert.doesNotMatch(elements.get("#valueContent").innerHTML, /역배 PICK/);
  assert.match(elements.get("#valueContent").innerHTML, /배당 갱신 60분 초과/);
});

test("changing dates cancels the previous page's expiration timer", () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, timers } = browser(now);
  context.payload = payload(now.value);
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.equal(timers.size, 1);
  vm.runInContext("changeDate(1)", context);
  assert.equal(timers.size, 0);
});
