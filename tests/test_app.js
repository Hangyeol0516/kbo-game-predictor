const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function browser(now) {
  const elements = new Map();
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  class Clock extends Date {
    static now() { return now.value; }
  }
  const context = vm.createContext({
    Date: Clock, Intl,
    fetch: url => { requests.push(url); return new Promise(() => {}); },
    clearTimeout: id => timers.delete(id),
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, {
          innerHTML: "", textContent: "", value: "",
          handlers: {},
          addEventListener(name, callback) { this.handlers[name] = callback; },
          setAttribute() {}, querySelectorAll: () => [],
        });
        return elements.get(selector);
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app.js"), "utf8"), context);
  return { context, elements, timers, requests };
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

test("historical recommendations are preserved and clearly labeled as past decisions", () => {
  const initial = Date.parse("2026-10-02T09:00:00Z");
  const now = { value: initial + 86400000 };
  const { context, elements, timers } = browser(now);
  context.payload = { ...payload(initial), viewMode: "historical" };
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.match(elements.get("#valueContent").innerHTML, /당시 역배 PICK/);
  assert.match(elements.get("#dataNotice").innerHTML, /저장된 당시 예측/);
  assert.doesNotMatch(elements.get("#dataNotice").innerHTML, /LIVE DATA/);
  assert.equal(timers.size, 0);
});

test("missing historical records are not presented as a date without games", () => {
  const { context, elements } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  context.payload = { viewMode: "historical", games: [], hitters: {}, updatedAt: null,
    methodVersion: "—", source: "저장된 예측", message: "예측이 저장되지 않았습니다." };
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.match(elements.get("#gameGrid").innerHTML, /저장된 당시 예측이 없습니다/);
  assert.doesNotMatch(elements.get("#gameGrid").innerHTML, /KBO 경기가 없습니다/);
});

test("reanalysis selection is explicit and date navigation resets to the default view", () => {
  const { context, elements, requests } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  vm.runInContext('state.date = new Date("2026-09-29T12:00:00")', context);
  elements.get("#reanalysisButton").handlers.click();
  assert.match(requests.at(-1), /date=2026-09-29&mode=reanalysis/);
  vm.runInContext("changeDate(1)", context);
  assert.match(requests.at(-1), /date=2026-09-30&mode=auto/);
});

test("incomplete data warnings are visible and escaped", () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements } = browser(now);
  context.payload = { ...payload(now.value), dataQuality: { warnings: ["선발 유형 미확인 <test>"] } };
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.match(elements.get("#dataNotice").innerHTML, /선발 유형 미확인 &lt;test&gt;/);
});
