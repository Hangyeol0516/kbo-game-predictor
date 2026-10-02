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
    valueBet:{available:true,recommendation:true,bookmakerCount:2,lastUpdate:at,commenceTime:new Date(now+4*3600000).toISOString(),
      favorite:metrics(away,54,1.55),underdog:metrics(home,46,2.7),returnAdvantagePp:20,
      quality:{marketFresh:true,bettingOpen:true,enoughBookmakers:true},
      criterion:{maximumAgeMinutes:60,closeBeforeMinutes:10,minimumBookmakers:2,minimumEvPct:8,minimumEdgePp:5,minimumAdvantagePp:10}} });
  const hitters = {'전체':[player('두산','선수 하나','one',81),player('LG','선수 둘','one',79),player('NC','선수 셋','two',75)]};
  const payload = {date:today, updatedAt:at, sources:['KBO 공식 홈페이지'], source:'KBO 공식 홈페이지', methodVersion:'test',
    lineupStatus:'confirmed', valueBetStatus:'connected',viewMode:'live',games:[game('one','LG','두산'),game('two','삼성','NC')],hitters,
    hittersByGame:{one:{'전체':hitters['전체'].slice(0,2)},two:{'전체':hitters['전체'].slice(2)}},dataQuality:{warnings:[]}};
  const performance={ evaluatedGames:100,decidedGames:100,correctGames:60,accuracy:60,brierScore:.23,modelVersion:'test',
    accuracyInterval95:[50.2,69.1],baseline:{homeWinAccuracy:51},calibration:[{predicted:55,observed:60,samples:100}],
    valueBet:{recommended:10,settled:10,wins:4,roi:8,profitUnits:.8},recent:[{date:today,away:'LG',home:'두산',score:'4 : 2',pick:'LG',winner:'LG',correct:true}] };
  let fail = false;
  const report=[];
  for (const width of [1440,390,320]) {
    const context=await browser.newContext({viewport:{width,height:1000},timezoneId:'America/Los_Angeles',reducedMotion:'reduce'});
    const page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/analysis?*', async route=>{
      if(fail) return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'테스트 연결 실패'})});
      const selected=new URL(route.request().url()).searchParams.get('date');
      const historical=selected<today;
      const data=JSON.parse(JSON.stringify(payload));data.date=selected;
      if(historical){data.viewMode='historical';data.games[0].predictionAt=at;data.games[0].result={awayScore:4,homeScore:2,winner:'LG',correct:true};
        data.games[0].predictionHistory=[{at,lineupStatus:'projected',homeProb:46,recommended:true,underdog:'두산',odds:2.7,modelVersion:'test'},
        {at,lineupStatus:'confirmed',homeProb:47,recommended:false,modelVersion:'test'}];}
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.route('**/api/performance*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(performance)}));
    await page.goto(`${base}/?date=${today}`);
    await page.locator('.game-card').first().waitFor();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('skip-link')), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>document.activeElement.id), 'games');
    await page.locator('#nativeDate').focus();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth), true, `date focus overflow at ${width}`);
    await page.locator('#refreshButton').focus();
    assert.equal(await page.locator('.game-card').count(),2);
    assert.equal(await page.locator('.value-card.recommended').count(),2);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,`overflow at ${width}`);
    const initialAxe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    assert.deepEqual(initialAxe.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)})), [], `initial accessibility at ${width}`);
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
