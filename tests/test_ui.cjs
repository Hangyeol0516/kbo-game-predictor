const { chromium } = require('playwright');
const { default: AxeBuilder } = require('@axe-core/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.PLAYBALL_UI_BASE_URL || 'http://127.0.0.1:18080';
const output = process.env.PLAYBALL_UI_ARTIFACT_DIR;
if (output) fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.PLAYBALL_CHROMIUM_PATH || undefined, args: ['--no-sandbox'] });
  const now = Date.now();
  const at = new Date(now).toISOString();
  const today = new Date(now + 9 * 3600000).toISOString().slice(0,10);
  const player = (team, name, gameId, probability) => ({ team, name, gameId, probability, gameTime: '18:30',
    position: '중견수', rawPosition: '중견수', opponent: 'vs 두산', pitcher: '상대 선발 테스트', order: '1번 타자',
    avg: .301, ab: 350, matchupAvg: .32, matchupAb: 130, pitcherHand: '우투', lineupConfirmed: true });
  const metrics = (team, probability, odds) => ({team, modelProbability: probability, marketProbability: probability-5,
    edgePp: 5, expectedReturnPct: 24, odds, bookmaker: 'Test', lastUpdate: at });
  const game = (id, away, home) => ({ id, time:'18:30', park:'잠실', away, home,
    awayPitcher:'테스트선발', homePitcher:'테스트선발', awayProb:54, homeProb:46, pick:away, confidence:'접전',
    startsAt: new Date(now+4*3600000).toISOString(), status:'scheduled', lineupConfirmed:true,
    reasons:['시즌 기록 기준 분석', '<script>공격 문자열</script>'], weather:{summary:'맑음'},
    lineups: Object.fromEntries(['away','home'].map(side=>[side,Array.from({length:9},(_,index)=>({order:index+1,name:`${side} 선수 ${index+1}`,position:'중견수'}))])),
    metrics:{awayRpg:4.8,homeRpg:4.2,awayEra:3.5,homeEra:4.1,awayStarterEra:2.9,homeStarterEra:null,awayBullpenPitches3d:180,homeBullpenPitches3d:210},
    predictionHistory:[{at:new Date(now-3600000).toISOString(),homeProb:44,modelVersion:'test-old',lineupStatus:'projected',recommended:true,underdog:home,odds:2.7,changes:['첫 저장']},
      {at,homeProb:46,modelVersion:'test',lineupStatus:'confirmed',recommended:false,changes:['모델 변경','라인업 상태 변경','추천 철회'],reasons:['저장된 실제 근거 <script>']}],
    valueBet:{available:true,recommendation:true,bookmakerCount:2,lastUpdate:at,commenceTime:new Date(now+4*3600000).toISOString(),
      favorite:metrics(away,54,1.55),underdog:metrics(home,46,2.7),returnAdvantagePp:20,
      quality:{marketFresh:true,bettingOpen:true,enoughBookmakers:true},
      criterion:{maximumAgeMinutes:60,closeBeforeMinutes:10,minimumBookmakers:2,minimumEvPct:8,minimumEdgePp:5,minimumAdvantagePp:10}} });
  const players = [player('두산','선수 하나','one',81),player('LG','선수 둘','one',79),player('NC','선수 셋','two',75)];
  const hitters = {'전체':players,'포수':players};
  const payload = {date:today, updatedAt:at, sources:['KBO 공식 홈페이지'], source:'KBO 공식 홈페이지', methodVersion:'test',
    lineupStatus:'confirmed', valueBetStatus:'connected',viewMode:'live',games:[game('one','LG','두산'),game('two','삼성','NC')],hitters,
    hittersByGame:{one:{'전체':players.slice(0,2),'포수':players.slice(0,2)},
      two:{'전체':players.slice(2),'포수':players.slice(2)}},dataQuality:{warnings:[]}};
  const performance={ evaluatedGames:100,decidedGames:100,correctGames:60,accuracy:60,brierScore:.23,modelVersion:'test',
    monthly:[{month:today.slice(0,7),decidedGames:100,dates:10,accuracy:60,brierScore:.23}],
    pagination:{page:1,pages:10,total:100},filters:{start:null,end:null,team:null},
    modelBreakdown:[{modelVersion:'test',evaluatedGames:100}],
    evaluationStatus:{message:'표본 부족 · 최소 100경기와 20개 경기일을 기다리고 있습니다.',decidedGames:100,dates:10,sufficientSample:false},
    accuracyInterval95:[50.2,69.1],baseline:{homeWinAccuracy:51},calibration:[{predicted:55,observed:60,samples:100}],
    valueBet:{recommended:10,settled:10,wins:4,roi:8,profitUnits:.8},recent:[{date:today,away:'LG',home:'두산',score:'4 : 2',pick:'LG',winner:'LG',correct:true}] };
  let fail = false;
  const report=[];
  for (const width of [1440,390,320]) {
    const context=await browser.newContext({viewport:{width,height:1000},timezoneId:'America/Los_Angeles',reducedMotion:'reduce'});
    const page=await context.newPage();
    await page.clock.install({time:now});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    let holdAnalysis=false, expiryMode='', fixtureNow=now, lastPerformanceQuery, scheduleCase='normal';
    const heldRoutes=[];
    await page.route('**/api/analysis?*', async route=>{
      if(holdAnalysis){heldRoutes.push(route);return;}
      if(fail) return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'테스트 연결 실패'})});
      const selected=new URL(route.request().url()).searchParams.get('date');
      const historical=selected<today;
      const data=JSON.parse(JSON.stringify(payload));data.date=selected;
      if(scheduleCase==='cancelled'){
        data.games=[{id:'rain',away:'LG',home:'KIA',time:'17:00',park:'광주',status:'cancelled',officialStatus:'우천취소',
          homeProb:null,awayProb:null,pick:'—',confidence:'예측 보류',lineupConfirmed:false,snapshotEligible:false,
          awayPitcher:'미정',homePitcher:'미정',reasons:[],valueBet:{available:false,recommendation:false}}];
        data.hitters={};data.hittersByGame={};data.valueBetStatus='no-market';
      }else if(scheduleCase==='empty'){
        data.games=[];data.hitters={};data.hittersByGame={};
        data.collectorStatus={lastError:'results: OSError',enabled:true,intervalSeconds:1200};
      }
      if(expiryMode){
        for(const game of data.games){
          const updated=new Date(fixtureNow-(expiryMode==='price'?59*60000:0)).toISOString();
          game.valueBet.lastUpdate=updated;
          game.valueBet.favorite.lastUpdate=updated;game.valueBet.underdog.lastUpdate=updated;
          game.valueBet.commenceTime=new Date(fixtureNow+(expiryMode==='cutoff'?11:120)*60000).toISOString();
        }
      }
      if(historical){data.viewMode='historical';data.games[0].predictionAt=at;data.games[0].result={awayScore:4,homeScore:2,winner:'LG',correct:true};
        data.games[0].predictionHistory=[{at,lineupStatus:'projected',homeProb:46,recommended:true,underdog:'두산',odds:2.7,modelVersion:'test'},
        {at,lineupStatus:'confirmed',homeProb:47,recommended:false,modelVersion:'test'}];}
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.route('**/api/performance*',route=>{
      const query=new URL(route.request().url()).searchParams;lastPerformanceQuery=query;
      const data=JSON.parse(JSON.stringify(performance));
      data.filters={start:query.get('start'),end:query.get('end'),team:query.get('team')};
      data.pagination.page=Number(query.get('page')||1);
      data.recent[0].pick=data.pagination.page>1?'두산':'LG';
      data.recent[0].modelVersion='test';data.recent[0].lineupStatus='confirmed';
      return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto(`${base}/?date=${today}&position=${encodeURIComponent('포수')}`);
    await page.locator('.game-card').first().waitFor();
    await page.locator('.evaluation-note').waitFor();
    assert.match(await page.locator('.evaluation-note').textContent(),/표본 부족.*100경기 \/ 10개 경기일/);
    assert.equal(await page.locator('.position-button.active').textContent(),'포수');
    assert.equal(new URL(page.url()).searchParams.get('position'),'포수');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('skip-link')), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>document.activeElement.id), 'games');
    await page.locator('#nativeDate').focus();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth), true, `date focus overflow at ${width}`);
    await page.locator('#refreshButton').focus();
    assert.equal(await page.locator('.game-card').count(),2);
    assert.match(await page.locator('#summaryContent').textContent(),/최근 변경 2경기/);
    assert.equal(await page.locator('.value-card.recommended').count(),2);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,`overflow at ${width}`);
    const initialAxe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    assert.deepEqual(initialAxe.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)})), [], `initial accessibility at ${width}`);
    await page.locator('.detail-button').first().click();
    assert.equal(await page.locator('#gameDetail').evaluate(dialog=>dialog.open),true);
    assert.equal(await page.locator('.lineup-list li').count(),18);
    assert.match(await page.locator('#detailContent').textContent(),/미제공/);
    assert.match(await page.locator('#detailContent').textContent(),/추천 철회/);
    assert.equal(await page.locator('.history-chart line').count(),0);
    assert.equal(await page.locator('#detailContent script').count(),0);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.querySelector('#gameDetail').contains(document.activeElement)),true);
    const detailAxe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    assert.deepEqual(detailAxe.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)})), [], `detail accessibility at ${width}`);
    if(output) await page.screenshot({path:path.join(output,`detail-${width}.png`)});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#gameDetail').evaluate(dialog=>dialog.open),false);
    assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('detail-button')),true);
    await page.locator('.detail-button').first().click();
    await page.evaluate(()=>changeDate(-1));
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    assert.equal(await page.locator('#gameDetail').evaluate(dialog=>dialog.open),false);
    await page.locator('#todayButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    await page.locator('#performanceStart').fill(today.slice(0,7)+'-01');
    await page.selectOption('#performanceTeam','LG');
    await page.locator('#performanceApply').click();
    await page.waitForFunction(()=>document.querySelector('#performanceScope').textContent.includes('LG'));
    assert.equal(lastPerformanceQuery.get('team'),'LG');
    assert.equal(lastPerformanceQuery.get('start'),today.slice(0,7)+'-01');
    await page.locator('#performanceNext').click();
    await page.waitForFunction(()=>document.querySelector('#performancePage').textContent.startsWith('2 /'));
    assert.match(await page.locator('.performance-history').textContent(),/예측 두산/);
    await page.locator('#performanceReset').click();
    await page.waitForFunction(()=>document.querySelector('#performancePage').textContent.startsWith('1 /'));
    assert.equal(lastPerformanceQuery.has('team'),false);
    assert.equal(lastPerformanceQuery.has('start'),false);
    await page.selectOption('#hitterGame','two');
    assert.match(await page.locator('.hitter-name').textContent(),/선수 셋/);
    await page.selectOption('#hitterGame','all');
    await page.selectOption('#teamFilter','LG');
    assert.equal(await page.locator('.game-card').count(),1);
    assert.match(await page.locator('.hitter-name').textContent(),/선수 둘/);
    assert.match(page.url(),/team=LG/);
    await page.locator('.reason-toggle').click();
    assert.equal(await page.locator('.reason-toggle').getAttribute('aria-expanded'),'true');
    assert.match(await page.locator('.reasons').textContent(),/<script>/);
    assert.equal(await page.locator('.reasons script').count(),0);
    await page.locator('#refreshButton').click();
    await page.waitForFunction(()=>document.querySelector('#refreshButton').disabled===false);
    assert.equal(await page.locator('.reason-toggle').getAttribute('aria-expanded'),'true');
    const downloadPromise=page.waitForEvent('download');await page.locator('#exportButton').click();
    const download=await downloadPromise;assert.match(download.suggestedFilename(),/playball-.*\.json/);
    await page.locator('#prevDate').click();
    await page.locator('.prediction-timeline').waitFor({state:'attached'});
    assert.match(await page.locator('.game-status').textContent(),/예측 적중/);
    await page.locator('#todayButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    fail=true;await page.locator('#refreshButton').click();
    await page.locator('.refresh-error').waitFor();
    assert.equal(await page.locator('.value-card.recommended').count(),0);
    assert.equal(await page.locator('.game-card').count(),1);
    fail=false;
    await page.locator('#refreshButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    for(const mode of ['cutoff','price']){
      expiryMode=mode;fixtureNow=await page.evaluate(()=>Date.now());
      await page.locator('#refreshButton').click();
      await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
      assert.equal(await page.locator('.value-card.recommended').count(),1);
      holdAnalysis=true;
      const requested=page.waitForRequest('**/api/analysis?*');
      await page.locator('#refreshButton').click();await requested;
      await page.locator('.detail-button').first().click();
      await page.locator('.table-scroll').focus();
      await page.clock.fastForward(60001);
      assert.equal(await page.locator('#gameDetail').evaluate(dialog=>dialog.open),true);
      assert.equal(await page.evaluate(()=>document.querySelector('#gameDetail').contains(document.activeElement)),true);
      assert.equal(await page.locator('#refreshButton').isDisabled(),true);
      assert.equal(await page.locator('.value-card.recommended').count(),0,`${mode} during pending refresh at ${width}`);
      await page.clock.fastForward(120000);
      await page.locator('.refresh-error').waitFor();
      assert.match(await page.locator('.refresh-error').textContent(),/요청 시간이 초과/);
      assert.equal(await page.locator('#refreshButton').isDisabled(),false);
      assert.equal(await page.locator('.game-card').count(),1);
      assert.match(await page.locator('#detailContent').textContent(),/최신 수집 실패/);
      await page.keyboard.press('Escape');
      holdAnalysis=false;expiryMode='';
      for(const route of heldRoutes.splice(0)) await route.abort().catch(()=>{});
      await page.locator('#refreshButton').click();
      await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
      assert.equal(await page.locator('.value-card.recommended').count(),1);
      assert.equal(await page.locator('.refresh-error').count(),0);
    }
    scheduleCase='cancelled';await page.locator('#refreshButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    assert.match(await page.locator('.game-status').textContent(),/우천취소/);
    assert.equal(await page.locator('.probability-row').count(),0);
    assert.equal(await page.locator('.value-card.recommended').count(),0);
    await page.locator('.detail-button').click();
    assert.match(await page.locator('#detailContent').textContent(),/라인업 자료가 제공/);
    assert.match(await page.locator('#detailContent').textContent(),/저장된 경기 전 예측 이력이 없습니다/);
    const cancelAxe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    assert.deepEqual(cancelAxe.violations.map(v=>v.id),[],`cancel detail accessibility at ${width}`);
    await page.keyboard.press('Escape');
    scheduleCase='empty';await page.locator('#refreshButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    assert.match(await page.locator('#summaryContent').textContent(),/자동 수집 일부 실패.*20분 간격/);
    assert.match(await page.locator('#gameGrid').textContent(),/KBO 경기가 없습니다/);
    scheduleCase='normal';fail=true;await page.reload();
    await page.locator('#retryButton').waitFor();
    assert.match(await page.locator('#summaryContent').textContent(),/경기 유무를 확인하지 못했습니다/);
    fail=false;await page.locator('#retryButton').click();
    await page.waitForFunction(()=>!document.querySelector('#refreshButton').disabled);
    const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    report.push({width,errors,violations:axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))});
    if(output) await page.screenshot({path:path.join(output,`playball-${width}.png`),fullPage:true});
    assert.deepEqual(axe.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)})), [], `accessibility at ${width}`);
    assert.deepEqual(errors,[]);
    await context.close();
  }
  if(output) fs.writeFileSync(path.join(output,'ui-check.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
