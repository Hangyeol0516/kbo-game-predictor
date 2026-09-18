const teams = {
  LG: { name: "LG 트윈스", color: "#a50034" }, 두산: { name: "두산 베어스", color: "#131230" },
  한화: { name: "한화 이글스", color: "#f36f21" }, 삼성: { name: "삼성 라이온즈", color: "#1763b0" },
  KIA: { name: "KIA 타이거즈", color: "#c51a2d" }, 롯데: { name: "롯데 자이언츠", color: "#0d2240" },
  SSG: { name: "SSG 랜더스", color: "#ce0e2d" }, KT: { name: "KT 위즈", color: "#222222" },
  NC: { name: "NC 다이노스", color: "#315288" }, 키움: { name: "키움 히어로즈", color: "#6f263d" },
};
const state = { date: new Date(), position: "전체", data: null, loading: false, requestId: 0 };
const pad = (number) => String(number).padStart(2, "0");
const toInputDate = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const formatUpdated = (value) => new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));

function renderDate() {
  const weekdays = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"];
  document.querySelector("#dateText").textContent = `${state.date.getFullYear()}. ${pad(state.date.getMonth() + 1)}. ${pad(state.date.getDate())} ${weekdays[state.date.getDay()]}`;
  document.querySelector("#dateSubText").textContent = "KBO 공식 일정 기준";
  document.querySelector("#nativeDate").value = toInputDate(state.date);
}

function teamBlock(code, pitcher) {
  const team = teams[code] || { name: code, color: "#334139" };
  return `<div class="team"><div class="team-badge" style="background:${team.color}">${code}</div><span class="team-name">${team.name}</span><span class="pitcher">선발 ${pitcher}</span></div>`;
}

function renderLoading() {
  document.querySelector("#gameGrid").innerHTML = `<div class="empty-state loading-state"><strong>공식 기록을 분석하고 있습니다.</strong>일정·팀 기록·라인업을 불러오는 데 몇 초 정도 걸릴 수 있습니다.</div>`;
  document.querySelector("#hitterContent").innerHTML = `<div class="empty-state"><strong>선수 매치업 계산 중</strong>KBO 라인업과 시즌 기록을 결합하고 있습니다.</div>`;
  document.querySelector("#dataNotice").innerHTML = `<span>LIVE</span> KBO 공식 데이터를 불러오는 중입니다.`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> 분석 중`;
}

function renderError(message) {
  const markup = `<div class="empty-state error-state"><strong>데이터를 불러오지 못했습니다.</strong>${message}<br><button type="button" id="retryButton" class="retry-button">다시 시도</button></div>`;
  document.querySelector("#gameGrid").innerHTML = markup;
  document.querySelector("#hitterContent").innerHTML = `<div class="empty-state"><strong>선수 예측을 표시할 수 없습니다.</strong>실제 데이터가 없을 때는 샘플 값을 대신 보여주지 않습니다.</div>`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> 연결 오류`;
  document.querySelector("#retryButton")?.addEventListener("click", () => loadAnalysis(true));
}

function renderGames() {
  const grid = document.querySelector("#gameGrid");
  const games = state.data?.games || [];
  if (!games.length) {
    grid.innerHTML = `<div class="empty-state"><strong>이 날짜에는 KBO 경기가 없습니다.</strong>KBO 공식 일정에서 등록된 경기를 찾지 못했습니다.</div>`;
    return;
  }
  grid.innerHTML = games.map((game, index) => `
    <article class="game-card">
      <div class="game-meta"><span>${game.time} · ${game.park} 야구장</span><strong>GAME ${pad(index + 1)}</strong></div>
      <div class="matchup">${teamBlock(game.away, game.awayPitcher)}<div class="prediction"><small>STATS PICK</small><strong>${game.pick}</strong><span>${game.confidence}</span></div>${teamBlock(game.home, game.homePitcher)}</div>
      <div class="probability-row"><b>${game.awayProb}%</b><div class="probability-track"><i style="width:${game.awayProb}%"></i><i style="width:${game.homeProb}%"></i></div><b>${game.homeProb}%</b></div>
      <button class="reason-toggle" type="button" aria-expanded="false">실제 지표와 계산 근거 보기 <span>⌄</span></button>
      <ul class="reasons">${game.reasons.map(reason => `<li>${reason}</li>`).join("")}<li>예고 선발: ${game.away} ${game.awayPitcher} · ${game.home} ${game.homePitcher}</li></ul>
    </article>`).join("");
  grid.querySelectorAll(".reason-toggle").forEach((button) => button.addEventListener("click", () => {
    const card = button.closest(".game-card"); card.classList.toggle("open");
    button.setAttribute("aria-expanded", card.classList.contains("open"));
  }));
}

function renderFilters() {
  const filter = document.querySelector("#positionFilters");
  const positions = Object.keys(state.data?.hitters || {});
  if (!positions.includes(state.position)) state.position = positions[0] || "전체";
  filter.innerHTML = positions.map(position => `<button type="button" role="tab" aria-selected="${state.position === position}" class="position-button ${state.position === position ? "active" : ""}" data-position="${position}">${position}</button>`).join("");
  filter.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
    state.position = button.dataset.position; renderFilters(); renderHitters();
  }));
}

function renderHitters() {
  const content = document.querySelector("#hitterContent");
  const players = state.data?.hitters?.[state.position] || [];
  if (!players.length) {
    content.innerHTML = `<div class="empty-state"><strong>분석 가능한 타자가 없습니다.</strong>라인업 또는 시즌 타격 기록이 공개된 뒤 확인할 수 있습니다.</div>`;
    return;
  }
  const [first, ...rest] = players;
  const lineupLabel = first.lineupConfirmed ? "확정 라인업" : "최근 라인업";
  content.innerHTML = `
    <article class="hitter-feature" data-number="1">
      <div><div class="rank-label">NO. 1 · ${first.rawPosition}</div><h3 class="hitter-name">${first.name}</h3><div class="hitter-team">${teams[first.team]?.name || first.team}</div><div class="matchup-note"><span>${first.opponent}</span><span>${first.pitcher}</span><span>${first.order}</span><span>시즌 AVG ${first.avg.toFixed(3)} · ${lineupLabel}</span></div></div>
      <div class="probability-ring" style="background: conic-gradient(var(--green) ${first.probability * 3.6}deg, #263a31 0deg)"><div><strong>${first.probability}%</strong><span>1+ HIT</span></div></div>
    </article>
    <div class="hitter-runners">${rest.map((player, index) => `<article class="runner-card"><strong>0${index + 2}</strong><div><h3>${player.name} <small>· ${player.rawPosition}</small></h3><p>${teams[player.team]?.name || player.team} · ${player.opponent} · AVG ${player.avg.toFixed(3)} · ${player.order}</p></div><span class="runner-prob">${player.probability}%</span></article>`).join("")}</div>`;
}

function renderAnalysis() {
  renderGames(); renderFilters(); renderHitters();
  const status = state.data.lineupStatus === "confirmed" ? "확정 라인업 반영" : "최근 라인업 기준";
  document.querySelector("#lineupLegend").innerHTML = `<i></i> ${status}`;
  document.querySelector("#dataNotice").innerHTML = `<span>OFFICIAL DATA</span> 출처: KBO 공식 홈페이지 · ${formatUpdated(state.data.updatedAt)} 갱신 · ${state.data.methodVersion}`;
  document.querySelector("#liveStatus").innerHTML = `<i></i> ${formatUpdated(state.data.updatedAt)} 갱신`;
}

async function loadPerformance() {
  const container = document.querySelector("#performanceContent");
  try {
    const response = await fetch("/api/performance");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "성능 정보를 불러오지 못했습니다.");
    const metric = (value, suffix = "") => value === null ? "—" : `${value}${suffix}`;
    const recent = data.recent.length ? `<div class="performance-history"><h3>최근 채점 결과</h3>${data.recent.map(game => `
      <article class="result-row">
        <time>${game.date}</time><div><strong>${game.away} ${game.score} ${game.home}</strong><span>예측 ${game.pick} · 승리 ${game.winner}</span></div>
        <b class="result-badge ${game.correct === true ? "correct" : game.correct === false ? "wrong" : "tie"}">${game.correct === true ? "적중" : game.correct === false ? "실패" : "무승부"}</b>
      </article>`).join("")}</div>` : `<div class="performance-empty"><strong>첫 채점을 기다리는 중입니다.</strong><p>${data.message}</p></div>`;
    container.innerHTML = `<div class="metric-grid">
      <article><span>평가 경기</span><strong>${data.evaluatedGames}</strong><small>GAMES</small></article>
      <article><span>승패 적중률</span><strong>${metric(data.accuracy, "%")}</strong><small>${data.correctGames} / ${data.decidedGames}</small></article>
      <article><span>Brier Score</span><strong>${metric(data.brierScore)}</strong><small>낮을수록 정확</small></article>
    </div>${recent}`;
  } catch (error) {
    container.innerHTML = `<div class="empty-state error-state"><strong>성능 정보를 표시할 수 없습니다.</strong>${error.message}</div>`;
  }
}

async function loadAnalysis(force = false) {
  const requestId = ++state.requestId;
  state.loading = true; state.data = null; renderDate(); renderLoading(); renderFilters();
  try {
    const response = await fetch(`/api/analysis?date=${toInputDate(state.date)}${force ? "&refresh=1" : ""}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "알 수 없는 오류가 발생했습니다.");
    if (requestId !== state.requestId) return;
    state.data = payload; renderAnalysis(); loadPerformance();
  } catch (error) {
    if (requestId === state.requestId) renderError(error.message);
  } finally { state.loading = false; }
}

function changeDate(offset) {
  state.date = new Date(state.date.getFullYear(), state.date.getMonth(), state.date.getDate() + offset);
  loadAnalysis();
}
document.querySelector("#prevDate").addEventListener("click", () => changeDate(-1));
document.querySelector("#nextDate").addEventListener("click", () => changeDate(1));
document.querySelector("#todayButton").addEventListener("click", () => { state.date = new Date(); loadAnalysis(); });
document.querySelector("#datePicker").addEventListener("click", () => {
  const input = document.querySelector("#nativeDate");
  if (typeof input.showPicker === "function") input.showPicker(); else input.click();
});
document.querySelector("#nativeDate").addEventListener("change", (event) => {
  const [year, month, day] = event.target.value.split("-").map(Number);
  state.date = new Date(year, month - 1, day); loadAnalysis();
});
loadAnalysis();
