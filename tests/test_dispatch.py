import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.config import db_path
from app.modules.dispatch.checklist import build
from app.modules.reconcile.audit import reconcile


def test_run_end_to_end_attachment(client):
    result=client.post('/api/runs',json={'sample':'dirty'}).json()
    response=client.get(f"/api/runs/{result['id']}/export")
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.json()['result']['reconcile']['late_count'] == 1
    assert response.json()['manifest']['files'].keys() == {'orders','calendar'}


def test_persistence_and_replay(client):
    first=client.post('/api/runs',json={'sample':'dirty'}).json()
    verified=client.post(f"/api/runs/{first['id']}/rerun").json()
    assert verified == {**first,'reused':True}
    assert client.get(f"/api/runs/{first['id']}").json()['id'] == first['id']


def test_concurrent_idempotence(client):
    def create(_):
        return client.post('/api/runs',json={'sample':'clean'}).json()
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(create,range(4)))
    assert len({r['id'] for r in results})==1
    assert [r['reused'] for r in results].count(False)==1
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1


def test_unknown_archive(client):
    response=client.get('/api/runs/not-registered')
    assert response.status_code == 404
    assert response.json()['code'] == 'RUN_NOT_FOUND'


def test_invalid_source_untouched(client):
    response=client.post('/api/runs',json={'orders':'{}','calendar':{}})
    assert response.status_code==422
    assert response.json()['code']=='BAD_CALENDAR'
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0


def test_dispatch_sorted_and_attributed(dirty):
    stage=next(row for row in dirty['dispatch']['stages'] if row['stage']=='主舞台')
    order=stage['steps'][0]
    assert (order['id'],order['technician'],order['line'],order['stage_end'])==('A01','许茂',1,650)
    assert [r['start'] for r in stage['steps']]==sorted(r['start'] for r in stage['steps'])


def test_dispatch_does_not_lose_pending_evidence(dirty):
    work=next(row for row in dirty['dispatch']['deferred'] if row['id']=='X03')
    assert work['related']==['X02'] and work['status']=='pending'


def test_placement_sequence_is_decision_order(clean):
    seqs={j['id']:j['seq'] for j in clean['schedule']['jobs'] if j['status']=='scheduled'}
    assert sorted(seqs.values())==list(range(1,16))
    assert (seqs['A01'],seqs['B01'])==(1,4)
    steps=[s for stage in clean['dispatch']['stages'] for s in stage['steps']]
    assert all('seq' in s for s in steps)


def test_changeover_gap_and_handover(clean):
    changes=clean['dispatch']['changeovers']
    assert [(c['stage'],c['from_show'],c['to_show']) for c in changes]==[
        ('主舞台','合奏专场','后台保障'),('侧舞台','山海舞集','后台保障')]
    main=changes[0]
    assert (main['clear_end'],main['next_start'],main['gap'])==(840,840,0)
    assert main['continuous'] is True
    assert (main['last']['id'],main['next']['id'])==('A05','C04')
    assert main['last']['technician']==main['next']['technician']=='许茂'


def test_changeover_gap_counts_stage_reset():
    calendar={'date':'2026-11-20','technicians':{'S1':{'name':'许茂'}}}
    schedule={'jobs':[
        {'id':'F1','show':'前场','title':'拆景','line':1,'status':'scheduled','stage':'主舞台',
         'seq':1,'start':600,'end':630,'stage_end':645,'tech':'S1','needs':[]},
        {'id':'B1','show':'后场','title':'装台','line':2,'status':'scheduled','stage':'主舞台',
         'seq':2,'start':660,'end':700,'stage_end':700,'tech':'S1','needs':[]}]}
    audit={'collisions':[],'premature':[],'row_gap':0,'missing':[]}
    sheet=build(schedule,calendar,audit)
    change=sheet['changeovers'][0]
    assert (change['clear_end'],change['next_start'],change['gap'])==(645,660,15)
    assert sheet['release']=={'publishable':True,'blocks':[]}


def test_release_gate_blocks_open_teardown(dirty):
    release=dirty['dispatch']['release']
    assert release['publishable'] is False
    blocks={b['code']:b['related'] for b in release['blocks']}
    assert blocks['KEY_TEARDOWN_PENDING']==['X05']
    assert blocks['PENDING_JOBS']==['X03','X06']


def test_release_gate_open_when_chain_closed(clean):
    assert clean['dispatch']['release']=={'publishable':True,'blocks':[]}


def test_release_gate_blocks_collision(clean):
    jobs=[dict(x) for x in clean['schedule']['jobs']]
    jobs[1].update(start=600,end=645,stage_end=645)
    bad=reconcile(clean['orders'],{'jobs':jobs},clean['conflicts'])
    sheet=build({'jobs':jobs},clean['calendars'],bad)
    codes=[b['code'] for b in sheet['release']['blocks']]
    assert 'RESOURCE_COLLISION' in codes
    assert sheet['release']['publishable'] is False


def test_publish_is_idempotent(client):
    run=client.post('/api/runs',json={'sample':'clean'}).json()
    assert run['publication'] is None
    first=client.post(f"/api/runs/{run['id']}/publish")
    assert first.status_code==200
    stamp=first.json()['publication']
    assert stamp['digest']==run['manifest']['digest']
    assert first.json()['reused'] is False
    again=client.post(f"/api/runs/{run['id']}/publish").json()
    assert again['reused'] is True
    assert again['publication']==stamp
    assert client.get(f"/api/runs/{run['id']}").json()['publication']==stamp
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==1


def test_publish_blocked_until_teardown_done(client):
    run=client.post('/api/runs',json={'sample':'dirty'}).json()
    response=client.post(f"/api/runs/{run['id']}/publish")
    assert response.status_code==409
    assert response.json()['code']=='RELEASE_BLOCKED'
    assert 'X05' in response.json()['message']
    assert client.get(f"/api/runs/{run['id']}").json()['publication'] is None
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==0


def test_same_input_reuses_sheet_and_publication(client):
    one=client.post('/api/runs',json={'sample':'clean'}).json()
    two=client.post('/api/runs',json={'sample':'clean'}).json()
    assert two['id']==one['id'] and two['reused'] is True
    client.post(f"/api/runs/{one['id']}/publish")
    again=client.post(f"/api/runs/{two['id']}/publish").json()
    assert again['reused'] is True
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==1


def test_rerun_still_consistent_after_publish(client):
    run=client.post('/api/runs',json={'sample':'clean'}).json()
    client.post(f"/api/runs/{run['id']}/publish")
    verified=client.post(f"/api/runs/{run['id']}/rerun").json()
    assert verified['reused'] is True
    assert verified['publication'] is not None
