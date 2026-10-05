const teams = {
  LG: { name: "LG 트윈스", color: "#a50034" }, 두산: { name: "두산 베어스", color: "#131230" },
  한화: { name: "한화 이글스", color: "#f36f21" }, 삼성: { name: "삼성 라이온즈", color: "#1763b0" },
  KIA: { name: "KIA 타이거즈", color: "#c51a2d" }, 롯데: { name: "롯데 자이언츠", color: "#0d2240" },
  SSG: { name: "SSG 랜더스", color: "#ce0e2d" }, KT: { name: "KT 위즈", color: "#222222" },
  NC: { name: "NC 다이노스", color: "#315288" }, 키움: { name: "키움 히어로즈", color: "#6f263d" },
};
const todayKst = () => new Date(`${new Date(Date.now() + 9 * 3600000).toISOString().slice(0, 10)}T12:00:00`);
const state = { date: todayKst(), mode: "auto", position: "전체", team: "all", hitterGame: "all", data: null,
  loading: false, requestId: 0, lastChecked: 0, refreshError: "", expanded: new Set(), detailId: null, performancePage: 1 };
let detailOpener;
let analysisController, performanceController, refreshTimer, performanceRequestId = 0;
const REFRESH_INTERVAL = 5 * 60000;
let valueExpiryTimer;
const pad = (number) => String(number).padStart(2, "0");
const toInputDate = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const formatUpdated = (value) => new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Seoul" }).format(new Date(value));
const formatTimestamp = (value) => Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat("ko-KR", {
  year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", timeZone:"Asia/Seoul",
}).format(new Date(value)) : "시각 미제공";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));

function renderDate() {
  const weekdays = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"];
  document.querySelector("#dateText").textContent = `${state.date.getFullYear()}. ${pad(state.date.getMonth() + 1)}. ${pad(state.date.getDate())} ${weekdays[state.date.getDay()]}`;
  document.querySelector("#dateSubText").textContent = "한국 시각 · KBO 공식 일정";
  document.querySelector("#nativeDate").value = toInputDate(state.date);
  const past = toInputDate(state.date) < new Date(Date.now() + 9 * 3600000).toISOString().slice(0, 10);
  document.querySelector("#historyControls").hidden = !past;
  document.querySelector("#historyButton").setAttribute("aria-pressed", state.mode !== "reanalysis");
  document.querySelector("#reanalysisButton").setAttribute("aria-pressed", state.mode === "reanalysis");
}

function teamBlock(code, pitcher) {
  const team = teams[code] || { name: code, color: "#334139" };
  return `<div class="team"><div class="team-badge" style="background:${team.color}">${escapeHtml(code)}</div><span class="team-name">${escapeHtml(team.name)}</span><span class="pitcher">선발 ${escapeHtml(pitcher)}</span></div>`;
}

function renderLoading() {
  document.querySelector("#summaryContent").textContent = "선택 날짜의 공식 자료를 확인하고 있습니다. 수신 후 요약합니다.";
  document.querySelector("#gameGrid").innerHTML = `<div class="empty-state loading-state"><strong>공식 기록을 분석하고 있습니다.</strong>일정·팀 기록·라인업을 불러오는 데 몇 초 정도 걸릴 수 있습니다.</div>`;
  document.querySelector("#hitterContent").innerHTML = `<div class="empty-state"><strong>선수 매치업 계산 중</strong>KBO 라인업과 시즌 기록을 결합하고 있습니다.</div>`;
  document.querySelector("#valueContent").innerHTML = `<div class="empty-state"><strong>역배 기대수익 계산 중</strong>모델 승률과 시장 배당을 대조하고 있습니다.</div>`;
  document.querySelector("#dataNotice").innerHTML = `<span>LIVE</span> KBO 공식 데이터를 불러오는 중입니다.`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> 분석 중`;
}

function renderError(message) {
  document.querySelector("#summaryContent").textContent = "수집 실패 · 경기 유무를 확인하지 못했습니다. 새로 확인으로 다시 시도하세요.";
  const markup = `<div class="empty-state error-state"><strong>데이터를 불러오지 못했습니다.</strong>${escapeHtml(message)}<br><button type="button" id="retryButton" class="retry-button">다시 시도</button></div>`;
  document.querySelector("#gameGrid").innerHTML = markup;
  document.querySelector("#hitterContent").innerHTML = `<div class="empty-state"><strong>선수 예측을 표시할 수 없습니다.</strong>실제 데이터가 없을 때는 샘플 값을 대신 보여주지 않습니다.</div>`;
  document.querySelector("#valueContent").innerHTML = `<div class="empty-state"><strong>배당 가치를 표시할 수 없습니다.</strong>불완전한 데이터로 역배를 추천하지 않습니다.</div>`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> 연결 오류`;
  document.querySelector("#dataNotice").textContent = message;
  document.querySelector("#lineupLegend").textContent = "자료 미수신";
  document.querySelector("#gameSummary").textContent = "";
  document.querySelector("#retryButton")?.addEventListener("click", () => loadAnalysis());
}

function renderGames() {
  const grid = document.querySelector("#gameGrid");
  const games = visibleGames();
  document.querySelector("#gameSummary").textContent = `${games.length}경기${state.team !== "all" ? ` · ${teams[state.team].name}` : ""} · 확정 라인업 ${games.filter(game => game.lineupConfirmed).length}경기`;
  if (!games.length) {
    if ((state.data?.games || []).length && state.team !== "all") {
      grid.innerHTML = `<div class="empty-state"><strong>선택한 구단의 경기가 없습니다.</strong>전체 구단을 선택하면 다른 경기의 분석을 볼 수 있습니다.</div>`;
      return;
    }
    if (state.data?.viewMode === "historical") {
      grid.innerHTML = `<div class="empty-state"><strong>저장된 당시 예측이 없습니다.</strong>${escapeHtml(state.data.message)}</div>`;
      return;
    }
    grid.innerHTML = `<div class="empty-state"><strong>이 날짜에는 KBO 경기가 없습니다.</strong>KBO 공식 일정에서 등록된 경기를 찾지 못했습니다.</div>`;
    return;
  }
  grid.innerHTML = games.map((game, index) => `
    <article class="game-card ${state.expanded.has(game.id) ? "open" : ""} ${game.valueBet?.recommendation ? "value-pick" : ""}" data-game="${escapeHtml(game.id)}">
      <div class="game-meta"><span>${escapeHtml(game.time)} · ${escapeHtml(game.park)} 야구장 · ${escapeHtml(game.weather?.summary || "날씨 미제공")}</span><strong>${game.valueBet?.recommendation ? (state.data.viewMode === "historical" ? "당시 역배 추천" : "역배 EV+") : `GAME ${pad(index + 1)}`}</strong></div>
      <div class="matchup">${teamBlock(game.away, game.awayPitcher)}<div class="prediction"><small>${game.status === "cancelled" ? "공식 취소" : "STATS PICK"}</small><strong>${escapeHtml(game.pick)}</strong><span>${escapeHtml(game.confidence)}</span></div>${teamBlock(game.home, game.homePitcher)}</div>
      ${game.homeProb != null ? `<div class="probability-row"><b>${game.awayProb}%</b><div class="probability-track"><i style="width:${game.awayProb}%"></i><i style="width:${game.homeProb}%"></i></div><b>${game.homeProb}%</b></div>` : ""}
      <div class="game-status">${escapeHtml(gameStatus(game))}${game.result ? ` · ${escapeHtml(game.result.awayScore)} : ${escapeHtml(game.result.homeScore)}${state.data.viewMode === "historical" ? ` · ${game.result.correct === true ? "예측 적중" : game.result.correct === false ? "예측 실패" : "무승부"}` : ""}` : ""}</div>
      <button class="reason-toggle" type="button" aria-controls="reasons-${index}" aria-expanded="${state.expanded.has(game.id)}">실제 지표와 계산 근거 보기 <span>⌄</span></button>
      <button class="detail-button" type="button" data-detail="${escapeHtml(game.id)}" aria-haspopup="dialog">${escapeHtml(game.away)} @ ${escapeHtml(game.home)} 상세 · 라인업 · 변화</button>
      <ul id="reasons-${index}" class="reasons">${(game.reasons || []).map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}<li>예고 선발: ${escapeHtml(game.away)} ${escapeHtml(game.awayPitcher)} · ${escapeHtml(game.home)} ${escapeHtml(game.homePitcher)}</li>${game.predictionAt ? `<li>당시 예측 시각: ${escapeHtml(game.predictionAt)} · ${escapeHtml(game.modelVersion)}</li>` : ""}${game.predictionHistory?.length ? `<li>예측 변경 이력 (${game.predictionHistory.length}회)<ol class="prediction-timeline">${game.predictionHistory.map(event => `<li>${formatUpdated(event.at)} · ${event.lineupStatus === "confirmed" ? "확정" : "예상"} · 홈 승률 ${escapeHtml(event.homeProb)}% · ${event.recommended ? `${escapeHtml(event.underdog)} 추천 @ ${escapeHtml(event.odds)}` : "추천 없음"} · ${escapeHtml(event.modelVersion)}</li>`).join("")}</ol></li>` : ""}</ul>
    </article>`).join("");
  grid.querySelectorAll(".reason-toggle").forEach((button) => button.addEventListener("click", () => {
    const card = button.closest(".game-card"); card.classList.toggle("open");
    button.setAttribute("aria-expanded", card.classList.contains("open"));
    if (card.classList.contains("open")) state.expanded.add(card.dataset.game);
    else state.expanded.delete(card.dataset.game);
  }));
  grid.querySelectorAll(".detail-button").forEach(button => button.addEventListener("click", () => openGameDetail(button.dataset.detail, button)));
}

function renderDailySummary() {
  const games = visibleGames();
  const historical = state.data.viewMode === "historical";
  document.querySelector("#summary-title").textContent = `${state.data.date || toInputDate(state.date)} · ${historical ? "저장 당시" : state.data.viewMode === "reanalysis" ? "현재 기록 재분석" : "경기"} 요약`;
  const changes = games.flatMap(game => {
    const history = game.predictionHistory || [];
    if (history.length < 2) return [];
    const latest = history.at(-1);
    const meaningful = (latest.changes || []).filter(change => change !== "정기 확인 · 주요 항목 동일");
    return meaningful.length ? [`${game.away} @ ${game.home} · ${formatUpdated(latest.at)} 저장 · ${meaningful.join(" · ")}`] : [];
  });
  const missing = historical && !state.data.games.length;
  document.querySelector("#summaryContent").innerHTML = `<div class="summary-metrics">
    <div><strong>${missing ? "—" : state.data.games.length}</strong><span>${historical ? "저장된 경기" : "선택 날짜 경기"}</span></div>
    <div><strong>${missing ? "—" : games.length}</strong><span>${state.team === "all" ? "표시 경기" : escapeHtml(state.team) + " 경기"}</span></div>
    <div><strong>${games.filter(game => game.lineupConfirmed && game.status !== "cancelled").length}</strong><span>${historical ? "저장 확정 라인업" : "확정 라인업"}</span></div>
    <div><strong>${games.filter(game => historical ? game.result : game.status === "cancelled").length}</strong><span>${historical ? "종료 결과 수신" : "공식 취소"}</span></div>
  </div><p>${escapeHtml(statusGuidance())}</p><details class="summary-changes"><summary>저장 이력의 최근 변경 ${changes.length}경기</summary>${changes.length ? `<ul>${changes.map(change => `<li>${escapeHtml(change)}</li>`).join("")}</ul>` : `<p>비교 가능한 주요 변경이 없습니다. 첫 저장과 반복 조회를 변경으로 계산하지 않습니다.</p>`}<p>마지막 두 저장 시점을 비교합니다. 재분석 화면의 이력은 과거에 저장한 예측입니다.</p></details>`;
}

function statusGuidance() {
  if (state.refreshError) return "최신 수집 실패 · 이전 자료를 표시합니다. 추천은 보류합니다. 새로 확인으로 재시도하세요.";
  if (state.data?.viewMode === "historical") return state.data.games.length ? "경기 전에 저장한 자료입니다. 공식 결과가 수집되면 채점됩니다. 최신 일정 상태는 현재 기록 재분석에서 확인하세요." : "저장 예측이 없습니다. 경기 없음으로 판단할 수 없습니다. 재분석은 별도로 선택하세요.";
  if (state.data?.viewMode === "reanalysis") return "현재 기록을 사용한 분석입니다. 당시 예측과 구분하며 새 평가 기록으로 저장하지 않습니다.";
  const games = visibleGames();
  const collector = state.data?.collectorStatus;
  const collectorNotice = collector?.lastError ? (collector.enabled === false
    ? "자동 수집 일부 실패 · 자동 수집이 꺼져 있습니다. 새로 확인으로 화면 자료를 다시 요청하세요. "
    : `자동 수집 일부 실패 · ${Math.round((collector.intervalSeconds || 900) / 60)}분 간격의 다음 수집에서 재시도합니다. `) : "";
  if (!games.length) return collectorNotice + (state.data.games.length ? "관심 구단의 등록 경기가 없습니다. 전체 구단에서 다른 경기를 확인하세요." : "공식 일정 조회에 성공했으나 등록 경기가 없습니다. 다른 날짜를 선택하거나 새로 확인하세요.");
  const waiting = games.filter(game => !game.lineupConfirmed && game.status === "scheduled").length;
  const cancelled = games.filter(game => game.status === "cancelled").length;
  const follow = toInputDate(state.date) === toInputDate(todayKst()) && state.mode === "auto" && document.querySelector("#autoRefresh").checked ? "화면이 보이는 동안 5분마다 확인합니다." : "새로 확인으로 최신 자료를 요청할 수 있습니다.";
  return `${cancelled ? `공식 취소 ${cancelled}경기 · 상세에서 사유를 확인하세요. ` : ""}${waiting ? `라인업 공개 대기 ${waiting}경기 · 최근 라인업은 확정 명단이 아닙니다. ` : ""}${collectorNotice}${follow}`;
}

function historyMarkup(game) {
  const events = (game.predictionHistory || []).filter(event => Number.isFinite(event.homeProb) && Number.isFinite(Date.parse(event.at)))
    .sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  if (!events.length) return `<p class="detail-empty">저장된 경기 전 예측 이력이 없습니다. 현재 분석을 과거 이력으로 만들어 표시하지 않습니다.</p>`;
  const first = Date.parse(events[0].at), last = Date.parse(events.at(-1).at);
  const x = event => events.length === 1 ? 150 : 20 + (Date.parse(event.at) - first) / (last - first || 1) * 260;
  const y = event => 120 - Math.min(100, Math.max(0, event.homeProb));
  const lines = events.slice(1).map((event, index) => event.modelVersion === events[index].modelVersion
    ? `<line x1="${x(events[index])}" y1="${y(events[index])}" x2="${x(event)}" y2="${y(event)}" />` : "").join("");
  return `<p>${escapeHtml(game.home)} 홈 승률 · ${events.length === 1 ? "저장 1건: 추세를 판단할 수 없습니다." : "모델이 달라지는 구간은 선으로 연결하지 않습니다."} 변경 이유는 확인된 항목 차이이며 인과 설명은 아닙니다.</p>
    <svg class="history-chart" viewBox="0 0 300 140" aria-hidden="true"><path class="chart-axis" d="M20 20 V120 H280"/><text x="0" y="25">100</text><text x="4" y="122">0</text>${lines}${events.map(event => `<circle cx="${x(event)}" cy="${y(event)}" r="4" />`).join("")}</svg>
    <div class="table-scroll" tabindex="0" role="region" aria-label="예측 변화 내역"><table class="detail-table"><caption>저장 시점별 홈 승률과 추천 변화</caption><thead><tr><th scope="col">한국 시각 / 모델</th><th scope="col">승률 / 라인업</th><th scope="col">추천 / 변경</th></tr></thead><tbody>${events.map(event => `<tr><td>${escapeHtml(formatTimestamp(event.at))}<small>${escapeHtml(event.modelVersion)}</small></td><td>${event.homeProb}%<small>${event.lineupStatus === "confirmed" ? "확정" : "예상"}</small></td><td>${event.recommended ? `${escapeHtml(event.underdog)} @ ${escapeHtml(event.odds)}` : "추천 없음"}<small>${escapeHtml((event.changes || []).join(" · ") || "변경 이유 기록 없음")}</small>${event.reasons?.length ? `<details><summary>저장된 계산 근거</summary><ul>${event.reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></details>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderGameDetail() {
  const game = state.data?.games.find(game => game.id === state.detailId);
  if (!game) { closeGameDetail(); return; }
  const content = document.querySelector("#detailContent");
  const focusable = 'button,a,input,select,summary,[tabindex="0"]';
  const focusIndex = [...content.querySelectorAll(focusable)].indexOf(document.activeElement);
  const expandedDetails = [...content.querySelectorAll("details")].map(detail => detail.open);
  document.querySelector("#detailTitle").textContent = `${game.away} @ ${game.home} · 경기 상세`;
  const model = game.modelVersion || state.data.methodVersion;
  const lineup = side => {
    const players = game.lineups?.[side];
    if (!players?.length) return `<p class="detail-empty">전체 라인업 자료가 제공되거나 저장되지 않았습니다. 타자 순위로 명단을 대신 만들지 않습니다.</p>`;
    return `<ol class="lineup-list">${players.map(player => `<li><b>${escapeHtml(player.order)}번</b> ${escapeHtml(player.name)} <span>${escapeHtml(player.position)}</span></li>`).join("")}</ol>`;
  };
  const metrics = game.metrics || {};
  const show = (key, digits = 2) => Number.isFinite(metrics[key]) ? metrics[key].toFixed(digits) : "미제공";
  content.innerHTML = `<p class="detail-mode">${escapeHtml(gameStatus(game))} · ${escapeHtml(state.data.viewMode === "reanalysis" ? "현재 기록 재분석" : state.data.viewMode === "historical" ? "저장 당시" : "실시간 분석")}<br>${escapeHtml(formatTimestamp(game.predictionAt || state.data.updatedAt))} 한국 시각 · ${escapeHtml(model)}</p>
    <p>${escapeHtml(game.time)} · ${escapeHtml(game.park)} · ${escapeHtml(game.weather?.summary || "날씨 미제공")}</p><p class="detail-mode">${escapeHtml(statusGuidance())}</p>
    <section><h3>선발과 제공 라인업</h3><p>${game.lineupConfirmed ? "확정 라인업" : "최근 라인업 또는 공개 대기 · 확정 명단 아님"}</p><div class="detail-lineups">${["away", "home"].map(side => `<div><h4>${escapeHtml(game[side])} · 선발 ${escapeHtml(game[side + "Pitcher"] || "미정")}</h4>${lineup(side)}</div>`).join("")}</div></section>
    <section><h3>전력 비교</h3><table class="detail-table"><caption>제공된 실제 지표 · 누락은 미제공 표시</caption><thead><tr><th scope="col">지표</th><th scope="col">${escapeHtml(game.away)}</th><th scope="col">${escapeHtml(game.home)}</th></tr></thead><tbody>${[["득점 / 경기", "Rpg", 2], ["팀 ERA", "Era", 2], ["선발 ERA", "StarterEra", 2], ["불펜 3일 투구 수", "BullpenPitches3d", 0]].map(([label, key, digits]) => `<tr><th scope="row">${label}</th><td>${show("away" + key, digits)}</td><td>${show("home" + key, digits)}</td></tr>`).join("")}</tbody></table></section>
    <section><h3>예측 근거</h3>${game.reasons?.length ? `<ul>${game.reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : `<p>제공된 예측 근거가 없습니다.</p>`}</section>
    <section><h3>예측 변화</h3>${state.data.viewMode === "reanalysis" ? `<p>아래는 저장 당시 이력이며 현재 기록 재분석 점을 추가하지 않습니다.</p>` : ""}${historyMarkup(game)}</section>`;
  content.querySelectorAll("details").forEach((detail, index) => { detail.open = Boolean(expandedDetails[index]); });
  if (focusIndex >= 0) {
    const next = content.querySelectorAll(focusable)[focusIndex];
    (next || document.querySelector("#detailClose")).focus({preventScroll:true});
  }
}

function openGameDetail(id, opener) {
  state.detailId = id; detailOpener = opener; renderGameDetail();
  document.querySelector("#gameDetail").showModal();
  document.querySelector("#detailClose").focus();
}

function closeGameDetail() {
  const dialog = document.querySelector("#gameDetail");
  if (dialog.open) dialog.close();
  state.detailId = null;
}

function signed(value, suffix = "%") {
  return `${value > 0 ? "+" : ""}${value}${suffix}`;
}

function renderValueBets() {
  const container = document.querySelector("#valueContent");
  const games = visibleGames();
  const historical = state.data?.viewMode === "historical";
  if (state.data?.viewMode === "reanalysis") {
    container.innerHTML = `<div class="empty-state"><strong>현재 기록을 사용한 재분석입니다.</strong>당시 승률이나 배당 추천을 재현한 결과가 아니며, 성능 평가에 저장하지 않습니다.</div>`;
    return;
  }
  const available = games.filter(game => game.valueBet?.available);
  const picks = available.filter(game => game.valueBet.recommendation);
  if (state.data?.valueBetStatus === "not-configured") {
    container.innerHTML = `<div class="empty-state value-empty"><strong>배당 API가 아직 연결되지 않았습니다.</strong>배당 자료가 연결되면 실제 시장 가격과 모델 승률을 비교할 수 있습니다.</div>`;
    return;
  }
  if (state.data?.valueBetStatus === "provider-error") {
    container.innerHTML = `<div class="empty-state value-empty"><strong>배당 제공사 응답을 받지 못했습니다.</strong>기존 예측은 그대로 제공하며 다음 갱신 때 배당을 다시 확인합니다. 배당 자료가 복구되면 비교 결과를 다시 표시합니다.</div>`;
    return;
  }
  if (!available.length) {
    if (historical) {
      container.innerHTML = `<div class="empty-state"><strong>저장된 당시 배당 자료가 없습니다.</strong>현재 배당으로 과거 추천을 복원하지 않습니다.</div>`;
      return;
    }
    container.innerHTML = `<div class="empty-state value-empty"><strong>이 날짜의 사전 배당이 없습니다.</strong>배당 시장이 열리지 않았거나 이미 마감된 경기입니다.</div>`;
    return;
  }
  const sorted = [...available].sort((a, b) => b.valueBet.underdog.expectedReturnPct - a.valueBet.underdog.expectedReturnPct);
  const lastUpdate = sorted.map(game => game.valueBet.lastUpdate).filter(Boolean).sort().at(-1);
  container.innerHTML = `<div class="value-summary"><strong>${historical ? "저장 당시 · " : ""}${picks.length ? `${picks.length}경기 역배 추천` : "추천 없음"}</strong><span>${available.length}경기 배당 비교${lastUpdate ? ` · ${formatUpdated(lastUpdate)} 기준` : ""}</span></div>` + sorted.map(game => {
    const value = game.valueBet;
    const dog = value.underdog;
    const favorite = value.favorite;
    const criterion = value.criterion;
    const misses = [];
    if (state.refreshError) misses.push("최신 자료 확인 실패");
    if (dog.expectedReturnPct < criterion.minimumEvPct) misses.push(`EV ${signed(criterion.minimumEvPct)} 미달`);
    if (dog.edgePp < criterion.minimumEdgePp) misses.push(`엣지 ${signed(criterion.minimumEdgePp, "%p")} 미달`);
    if (value.returnAdvantagePp < criterion.minimumAdvantagePp) misses.push(`정배 대비 ${signed(criterion.minimumAdvantagePp, "%p")} 미달`);
    if (!value.quality?.enoughBookmakers) misses.push(`북메이커 ${criterion.minimumBookmakers}곳 미만`);
    if (!value.quality?.marketFresh) misses.push(`배당 갱신 ${criterion.maximumAgeMinutes}분 초과`);
    if (!value.quality?.bettingOpen) misses.push(`경기 ${criterion.closeBeforeMinutes}분 전 마감`);
    const verdict = value.recommendation ? (historical ? "저장 당시 추천 기준을 충족했습니다." : "추천 기준을 모두 충족해 정배 대신 선택할 가치가 있습니다.") : `보류: ${misses.join(" · ")}`;
    return `<article class="value-card ${value.recommendation ? "recommended" : "no-bet"}">
      <div class="value-card-head"><span>${escapeHtml(game.away)} @ ${escapeHtml(game.home)} · ${escapeHtml(game.time)}</span><strong>${historical ? "당시 " : ""}${value.recommendation ? "역배 PICK" : "NO BET"} · ${escapeHtml(dog.team)}</strong></div>
      <div class="value-metrics">
        <div><small>최고 배당</small><b>${dog.odds.toFixed(2)}</b><span>${escapeHtml(dog.bookmaker)}</span></div>
        <div><small>모델 / 시장</small><b>${dog.modelProbability}%</b><span>${dog.marketProbability}% · 엣지 ${signed(dog.edgePp, "%p")}</span></div>
        <div class="primary"><small>역배 기대수익</small><b>${signed(dog.expectedReturnPct)}</b><span>1만원당 기대 ${signed(Math.round(dog.expectedReturnPct * 100), "원")}</span></div>
        <div><small>정배 기대수익</small><b>${signed(favorite.expectedReturnPct)}</b><span>${escapeHtml(favorite.team)} @ ${favorite.odds.toFixed(2)}</span></div>
      </div>
      <p>정배 대비 기대수익 우위 <strong>${signed(value.returnAdvantagePp, "%p")}</strong> · ${value.bookmakerCount}개 북메이커 비교<br><span class="value-verdict">${escapeHtml(verdict)}</span></p>
    </article>`;
  }).join("");
}

function renderFilters() {
  const filter = document.querySelector("#positionFilters");
  const positions = Object.keys(visibleHitters());
  if (state.data && !positions.includes(state.position)) state.position = positions[0] || "전체";
  filter.innerHTML = positions.map(position => `<button type="button" aria-pressed="${state.position === position}" class="position-button ${state.position === position ? "active" : ""}" data-position="${escapeHtml(position)}">${escapeHtml(position)}</button>`).join("");
  filter.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
    state.position = button.dataset.position; syncViewUrl(); renderFilters(); renderHitters();
  }));
}

function renderHitters() {
  const content = document.querySelector("#hitterContent");
  const players = visibleHitters()[state.position] || [];
  if (!players.length) {
    content.innerHTML = `<div class="empty-state"><strong>분석 가능한 타자가 없습니다.</strong>라인업 또는 시즌 타격 기록이 공개된 뒤 확인할 수 있습니다.</div>`;
    return;
  }
  const [first, ...rest] = players;
  const lineupLabel = first.lineupConfirmed ? "확정 라인업" : "최근 라인업";
  const estimateLabel = player => player.estimated ? "시즌 기록 없음 · 리그 평균 추정" : player.matchupEstimated ? "유형별 기록 부족 · 시즌 기록 사용" : "시즌·유형별 기록 반영";
  content.innerHTML = `
    <article class="hitter-feature" data-number="1">
      <div><div class="rank-label">NO. 1 · ${escapeHtml(first.rawPosition)}</div><h3 class="hitter-name">${escapeHtml(first.name)}</h3><div class="hitter-team">${escapeHtml(teams[first.team]?.name || first.team)}</div><div class="matchup-note"><span>${escapeHtml(first.gameTime || "")} 경기</span><span>${escapeHtml(first.opponent)}</span><span>${escapeHtml(first.pitcher)}</span><span>${escapeHtml(first.order)}</span><span>시즌 AVG ${first.avg.toFixed(3)}</span><span>vs ${escapeHtml(first.pitcherHand)} AVG ${first.matchupAvg.toFixed(3)} (${first.matchupAb}타수)</span><span>${lineupLabel}</span><span>${escapeHtml(estimateLabel(first))}</span></div></div>
      <div class="probability-ring" style="background: conic-gradient(var(--green) ${first.probability * 3.6}deg, #263a31 0deg)"><div><strong>${first.probability}%</strong><span>1+ HIT</span></div></div>
    </article>
    <div class="hitter-runners">${rest.map((player, index) => `<article class="runner-card"><strong>0${index + 2}</strong><div><h3>${escapeHtml(player.name)} <small>· ${escapeHtml(player.rawPosition)}</small></h3><p>${escapeHtml(teams[player.team]?.name || player.team)} · ${escapeHtml(player.gameTime || "")} 경기 · ${escapeHtml(player.opponent)} · 시즌 ${player.avg.toFixed(3)} · ${escapeHtml(player.pitcherHand)} 상대 ${player.matchupAvg.toFixed(3)} · ${escapeHtml(player.order)} · ${escapeHtml(estimateLabel(player))}</p></div><span class="runner-prob">${player.probability}%</span></article>`).join("")}</div>`;
}

function updateValueBetTime() {
  if (state.data?.viewMode === "historical") return null;
  const now = Date.now();
  const deadlines = [];
  for (const game of state.data?.games || []) {
    const value = game.valueBet;
    if (!value?.available) continue;
    const priceTimes = [value.favorite.lastUpdate, value.underdog.lastUpdate].map(Date.parse);
    const freshUntil = Math.min(...priceTimes) + value.criterion.maximumAgeMinutes * 60000;
    const openUntil = Date.parse(value.commenceTime) - value.criterion.closeBeforeMinutes * 60000;
    value.quality.marketFresh = Number.isFinite(freshUntil) && now <= freshUntil;
    value.quality.bettingOpen = Number.isFinite(openUntil) && now < openUntil;
    value.quality.ageMinutes = priceTimes.every(Number.isFinite)
      ? Math.round(Math.max(0, ...priceTimes.map(updated => (now - updated) / 60000)) * 10) / 10 : null;
    value.recommendation = value.recommendation && !state.refreshError && value.quality.marketFresh && value.quality.bettingOpen;
    if (value.quality.marketFresh) deadlines.push(freshUntil + 1);
    if (value.quality.bettingOpen) deadlines.push(openUntil);
  }
  return deadlines.length ? Math.min(...deadlines) : null;
}

async function fetchJson(url, controller, timeoutMs) {
  let timedOut = false;
  let timeoutId;
  let rejectAbort;
  const aborted = new Promise((_, reject) => {
    rejectAbort = () => {
      if (timedOut) return;
      const error = new Error("요청이 취소됐습니다.");
      error.name = "AbortError";
      reject(error);
    };
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
  });
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      const error = new Error("요청 시간이 초과됐습니다. 다시 시도해 주세요.");
      error.name = "TimeoutError";
      reject(error);
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  });
  const request = (async () => {
    const response = await fetch(url, { signal: controller.signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "요청에 실패했습니다.");
    return payload;
  })();
  try {
    return await Promise.race([request, timeout, aborted]);
  } finally {
    clearTimeout(timeoutId);
    controller.signal.removeEventListener("abort", rejectAbort);
  }
}

function renderAnalysis() {
  const nextExpiry = updateValueBetTime();
  clearTimeout(valueExpiryTimer);
  if (nextExpiry !== null) {
    valueExpiryTimer = setTimeout(() => {
      if (state.data) renderAnalysis();
    }, Math.min(Math.max(nextExpiry - Date.now(), 1), 2147483647));
  }
  renderGames(); renderValueBets(); renderHitterGames(); renderFilters(); renderHitters(); renderDailySummary();
  if (state.detailId) renderGameDetail();
  const games = visibleGames();
  const status = !games.length ? (state.data.viewMode === "historical" ? "저장 기록 없음" : "등록 경기 없음")
    : games.every(game => game.status === "cancelled") ? "공식 취소"
    : games.filter(game => game.status !== "cancelled").every(game => game.lineupConfirmed) ? "확정 라인업 반영" : "라인업 공개 대기";
  document.querySelector("#lineupLegend").innerHTML = `<i></i> ${status}`;
  const modeLabel = state.data.viewMode === "historical" ? "저장된 당시 예측" : state.data.viewMode === "reanalysis" ? "현재 기록으로 재분석 · 당시 예측 아님" : "LIVE DATA";
  const updated = state.data.updatedAt ? formatUpdated(state.data.updatedAt) : "저장 기록 없음";
  const warnings = state.data.dataQuality?.warnings || [];
  document.querySelector("#dataNotice").innerHTML = `<span>${escapeHtml(modeLabel)}</span> 출처: ${(state.data.sources || [state.data.source]).map(escapeHtml).join(" · ")} · ${updated} 기준 · ${escapeHtml(state.data.methodVersion)}${warnings.length ? `<ul>${warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : ""}${state.refreshError ? `<p class="refresh-error">최신 자료 확인 실패 · 이전 분석을 표시합니다. ${escapeHtml(state.refreshError)}</p>` : ""}`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> ${state.refreshError ? "최신 자료 확인 실패" : state.data.viewMode === "historical" ? "저장된 예측" : updated + " 갱신"}`;
  syncViewUrl();
}

async function loadPerformance() {
  performanceController?.abort();
  const requestId = ++performanceRequestId;
  const controller = new AbortController();
  performanceController = controller;
  const container = document.querySelector("#performanceContent");
  const params = new URLSearchParams();
  if (document.querySelector("#performanceModel").value === "all") params.set("modelVersion", "all");
  for (const [key, selector] of [["start", "#performanceStart"], ["end", "#performanceEnd"], ["team", "#performanceTeam"]]) {
    const value = document.querySelector(selector).value;
    if (value && value !== "all") params.set(key, value);
  }
  if (state.performancePage > 1) params.set("page", state.performancePage);
  container.setAttribute("aria-busy", "true");
  container.innerHTML = `<div class="empty-state"><strong>지정한 범위의 성능을 확인하고 있습니다.</strong>이전 범위의 지표와 혼합하지 않습니다.</div>`;
  document.querySelector("#performancePrev").disabled = true;
  document.querySelector("#performanceNext").disabled = true;
  document.querySelector("#performanceScope").textContent = "범위 확인 중…";
  try {
    const data = await fetchJson(`/api/performance${params.size ? "?" + params : ""}`, controller, 30000);
    if (requestId !== performanceRequestId) return;
    const metric = (value, suffix = "") => value === null ? "—" : `${value}${suffix}`;
    const value = data.valueBet || { recommended: 0, settled: 0, wins: 0, roi: null, profitUnits: 0 };
    const pagination = data.pagination || {page: 1, pages: 1, total: data.evaluatedGames};
    state.performancePage = pagination.page;
    document.querySelector("#performancePage").textContent = `${pagination.page} / ${pagination.pages} · ${pagination.total}경기`;
    document.querySelector("#performancePrev").disabled = pagination.page <= 1;
    document.querySelector("#performanceNext").disabled = pagination.page >= pagination.pages;
    const filters = data.filters || {};
    document.querySelector("#performanceScope").textContent = `${filters.start || "전체 시작일"} ~ ${filters.end || "전체 종료일"} · ${filters.team || "전체 구단"} · ${data.modelVersion === "all" ? "전체 모델: 경기당 확정 우선·최신 예측 하나" : "현재 모델의 최종 예측"} · 지표와 채점 내역은 같은 범위입니다.`;
    const recent = data.recent.length ? `<div class="performance-history"><h3>채점 내역</h3>${data.recent.map(game => `
      <article class="result-row">
        <time>${escapeHtml(game.date)}</time><div><strong>${escapeHtml(game.away)} ${escapeHtml(game.score)} ${escapeHtml(game.home)}</strong><span>예측 ${escapeHtml(game.pick)} · 승리 ${escapeHtml(game.winner)} · ${escapeHtml(game.modelVersion || data.modelVersion)} · ${game.lineupStatus === "confirmed" ? "확정" : "예상"}</span></div>
        <b class="result-badge ${game.correct === true ? "correct" : game.correct === false ? "wrong" : "tie"}">${game.correct === true ? "적중" : game.correct === false ? "실패" : "무승부"}</b>
      </article>`).join("")}</div>` : `<div class="performance-empty"><strong>첫 채점을 기다리는 중입니다.</strong><p>${escapeHtml(data.message)}</p></div>`;
    container.innerHTML = `${data.evaluationStatus ? `<p class="evaluation-note">${escapeHtml(data.evaluationStatus.message)} · ${data.evaluationStatus.decidedGames}경기 / ${data.evaluationStatus.dates}개 경기일</p>` : ""}<div class="metric-grid">
      <article><span>평가 경기</span><strong>${data.evaluatedGames}</strong><small>${escapeHtml(data.modelVersion || "GAMES")}</small></article>
      <article><span>승패 적중률</span><strong>${metric(data.accuracy, "%")}</strong><small>${data.correctGames} / ${data.decidedGames}${data.accuracyInterval95 ? ` · 95% 구간 ${data.accuracyInterval95.join("–")}%` : ""}</small></article>
      <article><span>Brier Score</span><strong>${metric(data.brierScore)}</strong><small>낮을수록 정확 · 50:50 기준 0.25</small></article>
      <article><span>역배 추천 / 적중</span><strong>${value.recommended} / ${value.wins}</strong><small>${value.settled}건 정산</small></article>
      <article><span>역배 가상 ROI</span><strong>${metric(value.roi, "%")}</strong><small>${signed(value.profitUnits, " units")} · 최종 추천당 1단위</small></article>
      <article><span>홈팀 선택 기준선</span><strong>${metric(data.baseline?.homeWinAccuracy ?? null, "%")}</strong><small>모든 경기에서 홈팀을 골랐을 때</small></article>
    </div>${monthlyMarkup(data.monthly || [])}${recent}${data.modelBreakdown?.length ? `<details class="calibration-summary"><summary>평가에 선택된 모델별 경기 수</summary><ul>${data.modelBreakdown.map(model => `<li>${escapeHtml(model.modelVersion)} · ${model.evaluatedGames}경기</li>`).join("")}</ul></details>` : ""}${data.calibration?.length ? `<details class="calibration-summary"><summary>확률과 실제 결과 비교</summary><p>홈팀의 예측 승률별 실제 승률입니다. 표본이 적을수록 변동이 큽니다.</p><table><caption class="sr-only">홈팀 확률 보정 점검</caption><thead><tr><th scope="col">평균 예측</th><th scope="col">실제 승률</th><th scope="col">경기 수</th></tr></thead><tbody>${data.calibration.map(bucket => `<tr><td>${bucket.predicted}%</td><td>${bucket.observed}%</td><td>${bucket.samples}</td></tr>`).join("")}</tbody></table></details>` : ""}`;
  } catch (error) {
    if (requestId !== performanceRequestId || error.name === "AbortError") return;
    container.innerHTML = `<div class="empty-state error-state"><strong>성능 정보를 표시할 수 없습니다.</strong>${escapeHtml(error.message)}</div>`;
    document.querySelector("#performanceScope").textContent = "조회 실패 · 범위 적용을 눌러 다시 확인하세요.";
  } finally {
    if (requestId === performanceRequestId) container.setAttribute("aria-busy", "false");
  }
}

function monthlyMarkup(months) {
  if (!months.length) return `<p class="evaluation-note">선택 범위에 월별 채점 자료가 없습니다.</p>`;
  return `<section class="monthly-performance"><h3>월별 적중률</h3><p>같은 모델·기간·구단 범위의 승패 경기 기준입니다. 무승부는 분모에서 제외합니다. 작은 월별 표본은 추세의 증거가 아닙니다.</p><div class="monthly-bars" aria-hidden="true">${months.map(month => `<div><span>${escapeHtml(month.month)}</span><i style="width:${Number.isFinite(month.accuracy) ? Math.max(0, Math.min(100, month.accuracy)) : 0}%"></i><b>${month.accuracy ?? "—"}% · ${month.decidedGames}경기</b></div>`).join("")}</div><details><summary>월별 수치 표</summary><table><caption>월별 승패 경기 · 날짜 수 · 적중률 · Brier</caption><thead><tr><th scope="col">월</th><th scope="col">경기 / 날짜</th><th scope="col">적중률</th><th scope="col">Brier</th></tr></thead><tbody>${months.map(month => `<tr><th scope="row">${escapeHtml(month.month)}</th><td>${month.decidedGames} / ${month.dates}</td><td>${month.accuracy ?? "—"}%</td><td>${month.brierScore ?? "—"}</td></tr>`).join("")}</tbody></table></details></section>`;
}

function renderHitterGames() {
  const games = visibleGames();
  if (!games.some(game => game.id === state.hitterGame)) state.hitterGame = "all";
  const select = document.querySelector("#hitterGame");
  select.innerHTML = `<option value="all">전체 경기</option>` + games.map(game => `<option value="${escapeHtml(game.id)}">${escapeHtml(game.time)} · ${escapeHtml(game.away)} @ ${escapeHtml(game.home)}</option>`).join("");
  select.value = state.hitterGame; syncViewUrl();
}

function visibleGames() {
  return (state.data?.games || []).filter(game => state.team === "all" || game.away === state.team || game.home === state.team);
}

function visibleHitters() {
  if (state.team === "all" && state.hitterGame === "all") return state.data?.hitters || {};
  const result = {};
  const byGame = state.data?.hittersByGame;
  const games = visibleGames().filter(game => state.hitterGame === "all" || game.id === state.hitterGame);
  const rankings = byGame ? games.map(game => byGame[game.id] || {}) : [state.data?.hitters || {}];
  for (const ranking of rankings) {
    for (const [position, players] of Object.entries(ranking)) {
      result[position] ||= [];
      result[position].push(...players.filter(player => (state.team === "all" || player.team === state.team)
        && (state.hitterGame === "all" || player.gameId === state.hitterGame)));
    }
  }
  for (const position of Object.keys(result)) {
    result[position].sort((a, b) => b.probability - a.probability);
    result[position] = result[position].slice(0, 3);
    if (!result[position].length) delete result[position];
  }
  return result;
}

function gameStatus(game) {
  if (game.status === "cancelled") return `공식 취소 · ${game.officialStatus || "사유 미제공"}`;
  if (state.data?.viewMode === "historical") return game.result ? "종료 · 당시 예측 채점" : "저장된 경기 전 예측";
  if (game.status === "completed") return "종료 · 현재 기록으로 분석 · 채점 제외";
  if (game.status === "in-progress") return "경기 진행 · 현재 기록으로 분석 · 채점 제외";
  if (game.status === "scheduled" && Date.parse(game.startsAt) <= Date.now()) return "예정 시각 경과 · 최신 경기 상태 확인 필요";
  if (game.status === "unavailable") return "공식 경기 상태 미확인 · 새로 확인 필요 · 사전 예측 저장 제외";
  return game.lineupConfirmed ? "확정 라인업" : "라인업 공개 대기 · 최근 라인업 참고";
}

function syncViewUrl() {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("date", toInputDate(state.date));
  url.searchParams.set("team", state.team);
  for (const [key, value, defaultValue] of [["game", state.hitterGame, "all"], ["mode", state.mode, "auto"], ["position", state.position, "전체"]]) {
    if (value === defaultValue) url.searchParams.delete(key); else url.searchParams.set(key, value);
  }
  window.history.replaceState(null, "", url);
}

function readViewUrl() {
  if (typeof window === "undefined") return;
  const params = new URL(window.location.href).searchParams;
  const value = params.get("date");
  if (/^\d{4}-\d{2}-\d{2}$/.test(value || "")) {
    const date = new Date(`${value}T12:00:00`);
    if (Number.isFinite(date.getTime()) && toInputDate(date) === value) state.date = date;
  }
  try { state.team = localStorage.getItem("playball-team") || "all"; } catch {}
  if (params.has("team")) state.team = params.get("team");
  if (!teams[state.team]) state.team = "all";
  if (["auto", "historical", "reanalysis"].includes(params.get("mode"))) state.mode = params.get("mode");
  state.position = params.get("position") || "전체";
  state.hitterGame = params.get("game") || "all";
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (!document.querySelector("#autoRefresh").checked || toInputDate(state.date) !== toInputDate(todayKst())
      || state.mode !== "auto" || document.visibilityState === "hidden") return;
  refreshTimer = setTimeout(() => loadAnalysis({ silent: true }), REFRESH_INTERVAL);
}

async function loadAnalysis({ silent = false } = {}) {
  analysisController?.abort();
  const controller = new AbortController();
  analysisController = controller;
  const requestId = ++state.requestId;
  if (!silent || !state.data) clearTimeout(valueExpiryTimer);
  clearTimeout(refreshTimer);
  state.loading = true;
  if (!silent || !state.data) {
    closeGameDetail();
    state.data = null; state.refreshError = ""; state.expanded.clear(); renderLoading(); renderFilters();
  }
  renderDate(); syncViewUrl();
  document.querySelector("#gameGrid").setAttribute("aria-busy", "true");
  document.querySelector("#refreshButton").disabled = true;
  document.querySelector("#exportButton").disabled = !state.data;
  document.querySelector("#updateStatus").textContent = "확인 중…";
  try {
    const payload = await fetchJson(`/api/analysis?date=${toInputDate(state.date)}&mode=${state.mode}`, controller, 180000);
    if (requestId !== state.requestId) return;
    state.data = payload; state.refreshError = ""; state.lastChecked = Date.now(); renderAnalysis(); loadPerformance();
    document.querySelector("#updateStatus").textContent = `${formatUpdated(state.lastChecked)} 확인 · 한국 시각`;
  } catch (error) {
    if (requestId !== state.requestId || error.name === "AbortError") return;
    if (silent && state.data) {
      state.refreshError = error.message; renderAnalysis();
      document.querySelector("#liveStatus").textContent = "최신 자료 확인 실패";
    } else renderError(error.message);
    document.querySelector("#updateStatus").textContent = "확인 실패 · 다시 시도할 수 있습니다.";
  } finally {
    if (requestId === state.requestId) {
      state.loading = false;
      document.querySelector("#gameGrid").setAttribute("aria-busy", "false");
      document.querySelector("#refreshButton").disabled = false;
      document.querySelector("#exportButton").disabled = !state.data;
      scheduleRefresh();
    }
  }
}

function changeDate(offset) {
  state.mode = "auto";
  state.date = new Date(state.date.getFullYear(), state.date.getMonth(), state.date.getDate() + offset, 12);
  loadAnalysis();
}
document.querySelector("#prevDate").addEventListener("click", () => changeDate(-1));
document.querySelector("#nextDate").addEventListener("click", () => changeDate(1));
document.querySelector("#todayButton").addEventListener("click", () => { state.mode = "auto"; state.date = todayKst(); loadAnalysis(); });
document.querySelector("#historyButton").addEventListener("click", () => { state.mode = "historical"; loadAnalysis(); });
document.querySelector("#reanalysisButton").addEventListener("click", () => { state.mode = "reanalysis"; loadAnalysis(); });
document.querySelector("#refreshButton").addEventListener("click", () => loadAnalysis({ silent: Boolean(state.data) }));
document.querySelector("#autoRefresh").addEventListener("change", scheduleRefresh);
document.querySelector("#performanceModel").addEventListener("change", () => { state.performancePage = 1; loadPerformance(); });
document.querySelector("#performanceFilters").addEventListener("submit", event => {
  event.preventDefault(); state.performancePage = 1; loadPerformance();
});
document.querySelector("#performanceReset").addEventListener("click", () => {
  document.querySelector("#performanceStart").value = ""; document.querySelector("#performanceEnd").value = "";
  document.querySelector("#performanceTeam").value = "all"; document.querySelector("#performanceModel").value = "current";
  state.performancePage = 1; loadPerformance();
});
document.querySelector("#performancePrev").addEventListener("click", () => { state.performancePage = Math.max(1, state.performancePage - 1); loadPerformance(); });
document.querySelector("#performanceNext").addEventListener("click", () => { state.performancePage += 1; loadPerformance(); });
document.querySelector("#performanceTeam").innerHTML += Object.entries(teams).map(([code, team]) => `<option value="${escapeHtml(code)}">${escapeHtml(team.name)}</option>`).join("");
document.querySelector("#detailClose").addEventListener("click", closeGameDetail);
document.querySelector("#gameDetail").addEventListener("close", () => {
  state.detailId = null;
  if (detailOpener?.isConnected) detailOpener.focus();
  else document.querySelector("#refreshButton").focus();
});
document.querySelector("#hitterGame").addEventListener("change", event => {
  state.hitterGame = event.target.value; renderHitterGames(); renderFilters(); renderHitters();
});
document.querySelector("#datePicker").addEventListener("click", () => {
  const input = document.querySelector("#nativeDate");
  try { if (typeof input.showPicker === "function") input.showPicker(); else input.focus(); }
  catch { input.focus(); }
});
document.querySelector("#nativeDate").addEventListener("change", (event) => {
  const value = event.target.value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
  const date = new Date(`${value}T12:00:00`);
  if (!Number.isFinite(date.getTime()) || toInputDate(date) !== value) return;
  state.mode = "auto"; state.date = date; loadAnalysis();
});
document.querySelector("#teamFilter").innerHTML += Object.entries(teams).map(([code, team]) => `<option value="${escapeHtml(code)}">${escapeHtml(team.name)}</option>`).join("");
document.querySelector("#teamFilter").addEventListener("change", event => {
  state.team = teams[event.target.value] ? event.target.value : "all";
  try { localStorage.setItem("playball-team", state.team); } catch {}
  syncViewUrl(); if (state.data) renderAnalysis();
});
document.querySelector("#exportButton").addEventListener("click", () => {
  if (!state.data) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.data, null, 2)], { type: "application/json;charset=utf-8" }));
  const link = document.createElement("a"); link.href = url;
  link.download = `playball-${state.data.date}-${state.data.viewMode || "live"}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
if (typeof document.addEventListener === "function") document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { clearTimeout(refreshTimer); return; }
  if (!state.loading && Date.now() - state.lastChecked >= REFRESH_INTERVAL
      && document.querySelector("#autoRefresh").checked && state.mode === "auto"
      && toInputDate(state.date) === toInputDate(todayKst())) loadAnalysis({ silent: true });
  else scheduleRefresh();
});
readViewUrl();
document.querySelector("#teamFilter").value = state.team;
loadAnalysis();
