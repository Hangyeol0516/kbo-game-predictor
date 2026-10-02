import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import kbo_analysis as analysis
from storage import PredictionStore


class ProductViewsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PredictionStore(str(Path(self.temp.name) / 'views.db'))

    def seed(self, day, game_id, away='LG', home='두산', winner='LG', model='model-a'):
        at = day + 'T17:00:00+09:00'
        with self.store.connect() as db:
            db.execute('''INSERT INTO game_predictions
                (prediction_date,game_id,model_version,lineup_status,created_at,away_team,home_team,
                 away_probability,home_probability,predicted_winner,payload_json)
                VALUES (?,?,?,'confirmed',?,?,?,.6,.4,?,'{}')''', (day,game_id,model,at,away,home,away))
            db.execute('INSERT INTO game_results VALUES (?,?,?,?,?,?,?,?)',
                       (game_id,day,away,home,3,2,winner,at))
            db.execute('''INSERT INTO value_bet_predictions
                (prediction_date,game_id,model_version,lineup_status,created_at,favorite_team,underdog_team,
                 underdog_odds,model_probability,market_probability,expected_return,return_advantage,
                 bookmaker,bookmaker_count,market_quality_passed,betting_open,recommended)
                VALUES (?,?,?,'confirmed',?,?,?,2.5,.6,.4,.5,.7,'fixture',2,1,1,1)''',
                       (day,game_id,model,at,home,away))

    def test_period_team_and_model_filter_share_game_and_roi_denominators(self):
        self.seed('2026-04-01','a')
        self.seed('2026-05-01','b',winner='두산')
        self.seed('2026-05-02','c',away='삼성',home='NC',winner='삼성')
        self.seed('2026-05-03','d',model='model-b')
        result=self.store.performance_summary('model-a',start='2026-05-01',end='2026-05-31',team='LG')
        self.assertEqual((result['evaluatedGames'],result['accuracy']), (1,0))
        self.assertEqual((result['valueBet']['settled'],result['valueBet']['roi']), (1,-100))
        self.assertEqual(result['monthly'], [{'month':'2026-05','decidedGames':1,'dates':1,'accuracy':0,'brierScore':.36}])
        self.assertEqual(result['recent'][0]['modelVersion'],'model-a')
        self.assertEqual(result['evaluationStatus']['dates'],1)
        self.assertEqual(result['filters'],{'start':'2026-05-01','end':'2026-05-31','team':'LG'})

    def test_pagination_is_stable_and_does_not_change_summary(self):
        for n in range(1,24): self.seed(f'2026-05-{n:02}', f'g{n:02}')
        first=self.store.performance_summary('model-a',page=1)
        second=self.store.performance_summary('model-a',page=2)
        last=self.store.performance_summary('model-a',page=999)
        self.assertEqual(first['evaluatedGames'],second['evaluatedGames'])
        self.assertEqual(first['valueBet'],second['valueBet'])
        self.assertEqual(first['monthly'],second['monthly'])
        self.assertEqual(last['pagination'],{'page':3,'pageSize':10,'pages':3,'total':23})
        ids=[r['gameId'] for page in (first,second,last) for r in page['recent']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(len(ids),23)
        self.assertEqual(ids[0],'g23')

    def test_draws_are_visible_but_excluded_from_accuracy_and_monthly_decided(self):
        self.seed('2026-05-01','tie',winner=None)
        result=self.store.performance_summary()
        self.assertEqual(result['evaluatedGames'],1)
        self.assertEqual(result['decidedGames'],0)
        self.assertIsNone(result['monthly'][0]['accuracy'])
        self.assertEqual(result['valueBet']['settled'],0)
        self.assertIsNone(result['recent'][0]['correct'])

    def test_event_history_reports_confirmed_changes_and_stored_reasons(self):
        game={'homeProb':45,'awayPitcher':'투수A','homePitcher':'투수B',
              'reasons':['공식 기록 <근거>'],'valueBet':{'recommendation':True,'underdog':{'team':'두산','odds':2.5}}}
        with self.store.connect() as db:
            for n,(version,lineup) in enumerate([('m1','projected'),('m2','confirmed')]):
                event=deepcopy(game)
                if n:
                    event['homeProb']=48;event['awayPitcher']='투수C';event['valueBet']['recommendation']=False
                db.execute('''INSERT INTO prediction_events
                    (prediction_date,game_id,model_version,lineup_status,created_at,payload_json)
                    VALUES ('2026-05-01','fixture',?,?,?,?)''',
                    (version,lineup,f'2026-05-01T{16+n}:00:00+09:00',json.dumps(event)))
        history=self.store.prediction_history('2026-05-01')['fixture']
        self.assertEqual(history[0]['changes'],['첫 저장'])
        self.assertEqual(history[1]['changes'],['모델 변경','라인업 상태 변경','예고 선발 변경','추천 철회','승률 변경'])
        self.assertEqual(history[1]['reasons'],['공식 기록 <근거>'])
        self.assertEqual(self.store.prediction_history('2026-05-02'),{})

    def test_cancelled_schedule_preserves_reason_without_computing_predictions(self):
        games=[{'G_ID':'rain','AWAY_NM':'LG','HOME_NM':'두산','G_TM':'18:30','S_NM':'잠실',
                'GAME_STATE_SC':'4','CANCEL_SC_ID':'1','CANCEL_SC_NM':'우천취소'}]
        with patch('kbo_analysis.fetch_games',return_value=games), patch('kbo_analysis.fetch_team_stats') as teams:
            result=analysis._analyze_uncached('2026-05-01')
        teams.assert_not_called()
        self.assertEqual(result['games'][0]['officialStatus'],'우천취소')
        self.assertIsNone(result['games'][0]['homeProb'])
        self.assertFalse(result['games'][0]['snapshotEligible'])
        self.assertEqual(result['valueBets'],[])
        self.store.save_analysis(result)
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM prediction_events').fetchone()[0],0)

    def test_unknown_state_is_not_invented_as_rain_cancellation(self):
        self.assertFalse(analysis.cancelled_game({'GAME_STATE_SC':'9','CANCEL_SC_ID':'0','CANCEL_SC_NM':'정상경기'}))
        self.assertFalse(analysis.cancelled_game({'GAME_STATE_SC':'1'}))
        self.assertFalse(analysis.cancelled_game({'GAME_STATE_SC':'3','GAME_RESULT_CK':1,'CANCEL_SC_ID':'2','CANCEL_SC_NM':'강우콜드'}))
        self.assertFalse(analysis.cancelled_game({'GAME_STATE_SC':'2','CANCEL_SC_ID':'7','CANCEL_SC_NM':'일시중단'}))

    def test_all_model_scope_selects_one_game_but_reports_selected_model(self):
        self.seed('2026-05-01','same',model='old')
        with self.store.connect() as db:
            db.execute('''INSERT INTO game_predictions
                (prediction_date,game_id,model_version,lineup_status,created_at,away_team,home_team,
                 away_probability,home_probability,predicted_winner,payload_json)
                VALUES ('2026-05-01','same','new','projected','2026-05-01T18:00:00+09:00','LG','두산',.4,.6,'두산','{}')''')
        all_models=self.store.performance_summary()
        self.assertEqual(all_models['evaluatedGames'],1)
        self.assertEqual(all_models['modelBreakdown'],[{'modelVersion':'old','evaluatedGames':1}])
        new=self.store.performance_summary('new')
        self.assertEqual(new['accuracy'],0)
        self.assertEqual(new['recent'][0]['modelVersion'],'new')

    def test_empty_filter_scope_preserves_empty_totals_and_one_page(self):
        self.seed('2026-05-01','fixture')
        result=self.store.performance_summary('model-a',team='한화',page=2)
        self.assertEqual(result['evaluatedGames'],0)
        self.assertEqual(result['valueBet']['recommended'],0)
        self.assertEqual(result['monthly'],[])
        self.assertEqual(result['recent'],[])
        self.assertEqual(result['pagination'],{'page':1,'pageSize':10,'pages':1,'total':0})

    def test_actual_lineup_membership_change_is_distinguished_from_status(self):
        with self.store.connect() as db:
            for n,name in enumerate(['기존타자','대체타자']):
                game={'homeProb':45,'lineups':{'away':[{'name':name,'order':1,'position':'중견수'}],'home':[]}}
                db.execute('''INSERT INTO prediction_events
                    (prediction_date,game_id,model_version,lineup_status,created_at,payload_json)
                    VALUES ('2026-05-01','lineup','same','confirmed',?,?)''',
                    (f'2026-05-01T{16+n}:00:00+09:00',json.dumps(game)))
        self.assertEqual(self.store.prediction_history('2026-05-01')['lineup'][1]['changes'],['라인업 명단 변경'])

    def test_performance_and_roi_share_snapshot_during_concurrent_result_correction(self):
        self.seed('2026-05-01','concurrent')
        original=self.store.evaluated_predictions
        def change_result(*args,**kwargs):
            rows=original(*args,**kwargs)
            with self.store.connect() as db:
                db.execute("UPDATE game_results SET winner='두산' WHERE game_id='concurrent'")
            return rows
        with patch.object(self.store,'evaluated_predictions',side_effect=change_result):
            result=self.store.performance_summary('model-a')
        self.assertEqual(result['accuracy'],100)
        self.assertEqual(result['valueBet']['wins'],1)
        fresh=self.store.performance_summary('model-a')
        self.assertEqual(fresh['accuracy'],0)
        self.assertEqual(fresh['valueBet']['wins'],0)
