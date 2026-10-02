const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function browser(now, { href, storedTeam } = {}) {
  const elements = new Map();
  const timers = new Map();
  const requests = [];
  const requestOptions = [];
  const documentHandlers = {};
  let timerId = 0;
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now.value])); }
    static now() { return now.value; }
  }
  const context = vm.createContext({
    Date: Clock, Intl, AbortController,
    fetch: (url, options) => { requests.push(url); requestOptions.push(options); return new Promise(() => {}); },
    URL,
    localStorage: { getItem: () => storedTeam || null, setItem() {} },
    clearTimeout: id => timers.delete(id),
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    document: {
      visibilityState: "visible",
      addEventListener(name, callback) { documentHandlers[name] = callback; },
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, {
          innerHTML: "", textContent: "", value: "", checked: false,
          handlers: {},
          addEventListener(name, callback) { this.handlers[name] = callback; },
          insertAdjacentHTML(position, html) { this.innerHTML += html; },
          setAttribute() {}, querySelectorAll: () => [],
        });
        return elements.get(selector);
      },
    },
  });
  if (href) context.window = { location: { href }, history: { replaceState(a, b, url) { context.window.location.href = String(url); } } };
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app.js"), "utf8"), context);
  return { context, elements, timers, requests, requestOptions, documentHandlers };
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
  const timer = [...timers.values()].find(timer => timer.delay === 60000);
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
  const timer = [...timers.values()].find(timer => timer.delay === 60001);
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
  const expiry = [...timers.keys()].find(id => timers.get(id).delay === 60000);
  assert.ok(expiry);
  vm.runInContext("changeDate(1)", context);
  assert.equal(timers.has(expiry), false);
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
  assert.equal([...timers.values()].some(timer => timer.delay === 60000 || timer.delay === 60001), false);
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

test("initial date and updated clock use Korea even when the browser runs in UTC", () => {
  const { context, elements, requests } = browser({ value: Date.parse("2026-10-02T16:00:00Z") });
  assert.match(requests[0], /date=2026-10-03/);
  assert.equal(vm.runInContext('formatUpdated("2026-10-02T16:00:00Z")', context), "오전 01:00");
  elements.get("#todayButton").handlers.click();
  assert.match(requests.at(-1), /date=2026-10-03/);
});

test("share links restore date, team and history mode and discard impossible dates", () => {
  const now = { value: Date.parse("2026-10-02T16:00:00Z") };
  const valid = browser(now, { href: "http://localhost/?date=2026-09-29&team=LG&mode=historical" });
  assert.match(valid.requests[0], /date=2026-09-29&mode=historical/);
  assert.equal(vm.runInContext("state.team", valid.context), "LG");
  const invalid = browser(now, { href: "http://localhost/?date=2026-02-30&team=unknown&mode=bad" });
  assert.match(invalid.requests[0], /date=2026-10-03&mode=auto/);
  assert.equal(vm.runInContext("state.team", invalid.context), "all");
});

test("shared hitter position survives initial loading and falls back only after data arrives", () => {
  const { context, elements } = browser({ value: Date.parse("2026-10-02T09:00:00Z") },
    { href: "http://localhost/?position=포수" });
  assert.equal(vm.runInContext("state.position", context), "포수");
  assert.match(context.window.location.href, /position=%ED%8F%AC%EC%88%98/);
  context.payload = { ...payload(Date.parse("2026-10-02T09:00:00Z")), hitters: { "전체": [], "포수": [] } };
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  assert.equal(vm.runInContext("state.position", context), "포수");
  assert.equal(new URL(context.window.location.href).searchParams.get("position"), "포수");
  context.payload.hitters = { "전체": [] };
  vm.runInContext("renderAnalysis()", context);
  assert.equal(vm.runInContext("state.position", context), "전체");
  assert.doesNotMatch(context.window.location.href, /position=/);
  assert.match(elements.get("#positionFilters").innerHTML, /전체/);
});

test("team filtering uses game-level hitter lists instead of filtering an already truncated global top three", () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements } = browser(now);
  context.payload = { ...payload(now.value), hitters: { "전체": [{ team: "두산", probability: 90 }] },
    hittersByGame: { one: { "전체": [{ team: "두산", probability: 90 }, { team: "LG", probability: 70 }] } } };
  context.payload.games[0].id = "one";
  vm.runInContext('state.data = payload; state.team = "LG"; renderGames()', context);
  assert.equal(vm.runInContext('visibleHitters()["전체"][0].probability', context), 70);
  vm.runInContext('state.team = "한화"; renderGames()', context);
  assert.match(elements.get("#gameGrid").innerHTML, /선택한 구단의 경기가 없습니다/);
});

test("date changes abort the previous request and a late response cannot replace the current date", async () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, requestOptions } = browser(now);
  const pending = [];
  context.fetch = url => url.startsWith("/api/performance")
    ? Promise.resolve({ ok: true, json: async () => ({ recent: [], valueBet: {}, evaluatedGames: 0 }) })
    : new Promise(resolve => pending.push(resolve));
  vm.runInContext("changeDate(1)", context);
  assert.equal(requestOptions[0].signal.aborted, true);
  vm.runInContext("changeDate(1)", context);
  pending[1]({ ok: true, json: async () => ({ ...payload(now.value), date: "2026-10-04" }) });
  await new Promise(resolve => setImmediate(resolve));
  pending[0]({ ok: true, json: async () => ({ ...payload(now.value), date: "2026-10-03" }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext("state.data.date", context), "2026-10-04");
});

test("background refresh failure keeps the previous display and suspends recommendations", async () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements } = browser(now);
  context.payload = payload(now.value);
  vm.runInContext('state.data = payload; state.expanded.add("one"); renderAnalysis()', context);
  context.fetch = async () => { throw new Error("一時 <error>"); };
  await vm.runInContext("loadAnalysis({ silent: true })", context);
  assert.match(elements.get("#gameGrid").innerHTML, /LG 트윈스/);
  assert.match(elements.get("#dataNotice").innerHTML, /이전 분석을 표시/);
  assert.match(elements.get("#dataNotice").innerHTML, /&lt;error&gt;/);
  assert.doesNotMatch(elements.get("#valueContent").innerHTML, /역배 PICK/);
  assert.match(elements.get("#valueContent").innerHTML, /최신 자료 확인 실패/);
  assert.equal(vm.runInContext('state.expanded.has("one")', context), true);
});

test("silent refresh keeps the existing cutoff timer running while the request is pending", async () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements, timers } = browser(now);
  context.payload = payload(now.value);
  vm.runInContext("state.data = payload; renderAnalysis()", context);
  const expiry = [...timers.entries()].find(([, timer]) => timer.delay === 60000);
  assert.ok(expiry);
  context.fetch = () => new Promise(() => {});
  const pending = vm.runInContext("loadAnalysis({ silent: true })", context);
  assert.ok([...timers.values()].some(timer => timer.delay === 60000));
  now.value += 60001;
  timers.delete(expiry[0]); expiry[1].callback();
  assert.doesNotMatch(elements.get("#valueContent").innerHTML, /역배 PICK/);
  assert.match(elements.get("#valueContent").innerHTML, /NO BET/);
  vm.runInContext("changeDate(1)", context);
  await pending;
});

test("analysis timeout covers JSON parsing and a later request can recover", async () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, elements, timers } = browser(now);
  let bodyStarted = false;
  let signal;
  context.fetch = async (_, options) => {
    signal = options.signal;
    return { ok: true, json: () => { bodyStarted = true; return new Promise(() => {}); } };
  };
  const pending = vm.runInContext("loadAnalysis()", context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(bodyStarted, true);
  const expired = [...timers.entries()].find(([, timer]) => timer.delay === 180000);
  assert.ok(expired);
  timers.delete(expired[0]); expired[1].callback();
  await pending;
  assert.equal(signal.aborted, true);
  assert.equal(timers.size, 0);
  assert.match(elements.get("#dataNotice").textContent, /요청 시간이 초과/);
  assert.equal(elements.get("#refreshButton").disabled, false);
  context.fetch = async () => ({ ok: true, json: async () => ({ ...payload(now.value), date: "2026-10-02",
    viewMode: "live", sources: ["test"], methodVersion: "test" }) });
  await vm.runInContext("loadAnalysis()", context);
  assert.match(elements.get("#gameGrid").innerHTML, /LG 트윈스/);
});

test("JSON body cancellation clears the previous deadline and ignores a late body", async () => {
  const now = { value: Date.parse("2026-10-02T09:00:00Z") };
  const { context, timers, elements } = browser(now);
  let finishBody;
  let signal;
  context.fetch = async (_, options) => {
    signal = options.signal;
    return { ok: true, json: () => new Promise(resolve => { finishBody = resolve; }) };
  };
  const pending = vm.runInContext("loadAnalysis()", context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof finishBody, "function");
  const previousTimeout = [...timers.keys()].find(id => timers.get(id).delay === 180000);
  context.fetch = () => new Promise(() => {});
  vm.runInContext("changeDate(1)", context);
  await pending;
  assert.equal(signal.aborted, true);
  assert.equal(timers.has(previousTimeout), false);
  finishBody({ ...payload(now.value), date: "2026-10-02" });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext("state.data", context), null);
  assert.match(elements.get("#gameGrid").innerHTML, /분석하고 있습니다/);
});

test("performance JSON timeout reports an error, clears its timer and can recover", async () => {
  const { context, elements, timers } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  let bodyStarted = false;
  let signal;
  context.fetch = async (_, options) => {
    signal = options.signal;
    return { ok: true, json: () => { bodyStarted = true; return new Promise(() => {}); } };
  };
  const pending = vm.runInContext("loadPerformance()", context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(bodyStarted, true);
  const expired = [...timers.entries()].find(([, timer]) => timer.delay === 30000);
  expired[1].callback();
  await pending;
  assert.equal(signal.aborted, true);
  assert.equal(timers.has(expired[0]), false);
  assert.match(elements.get("#performanceContent").innerHTML, /요청 시간이 초과/);
  context.fetch = async () => ({ ok: true, json: async () => ({ recent: [], evaluatedGames: 9, modelVersion: "recovered" }) });
  await vm.runInContext("loadPerformance()", context);
  assert.match(elements.get("#performanceContent").innerHTML, /recovered/);
  assert.equal([...timers.values()].some(timer => timer.delay === 30000), false);
});

test("date-change abort does not display a request error", async () => {
  const { context, elements } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  vm.runInContext("changeDate(1)", context);
  await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(elements.get("#dataNotice").innerHTML, /실패/);
});

test("automatic refresh runs for today's live view and pauses on hidden or historical pages", () => {
  const { context, elements, timers } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  elements.get("#autoRefresh").checked = true;
  vm.runInContext("scheduleRefresh()", context);
  assert.ok([...timers.values()].some(timer => timer.delay === 300000));
  context.document.visibilityState = "hidden";
  vm.runInContext("scheduleRefresh()", context);
  assert.equal([...timers.values()].some(timer => timer.delay === 300000), false);
  context.document.visibilityState = "visible";
  vm.runInContext('state.mode = "historical"; scheduleRefresh()', context);
  assert.equal([...timers.values()].some(timer => timer.delay === 300000), false);
});

test("clearing the date picker does not request an invalid date", () => {
  const { elements, requests } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  elements.get("#nativeDate").handlers.change({ target: { value: "" } });
  assert.equal(requests.length, 1);
});

test("scorecard response races keep the user's latest model selection", async () => {
  const { context, elements } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  const pending = [];
  const signals = [];
  context.fetch = (_, options) => { signals.push(options.signal); return new Promise(resolve => pending.push(resolve)); };
  vm.runInContext("loadPerformance()", context);
  elements.get("#performanceModel").value = "all";
  vm.runInContext("loadPerformance()", context);
  assert.equal(signals[0].aborted, true);
  pending[1]({ ok: true, json: async () => ({ modelVersion: "all", recent: [], evaluatedGames: 9 }) });
  await new Promise(resolve => setImmediate(resolve));
  pending[0]({ ok: true, json: async () => ({ modelVersion: "old", recent: [], evaluatedGames: 1 }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.get("#performanceContent").innerHTML, /all/);
  assert.doesNotMatch(elements.get("#performanceContent").innerHTML, />old</);
});

test("doubleheader hitter selection reads only the selected game's matchup", () => {
  const { context } = browser({ value: Date.parse("2026-10-02T09:00:00Z") });
  context.payload = { games: [{ id: "first", away: "LG", home: "두산" }, { id: "second", away: "LG", home: "두산" }],
    hitters: { "전체": [{ team: "LG", gameId: "first", probability: 90 }] },
    hittersByGame: { first: { "전체": [{ team: "LG", gameId: "first", probability: 90 }] },
                    second: { "전체": [{ team: "LG", gameId: "second", probability: 60 }] } } };
  vm.runInContext('state.data = payload; state.hitterGame = "second"', context);
  assert.equal(vm.runInContext('visibleHitters()["전체"][0].probability', context), 60);
});
