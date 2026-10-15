import fcntl
import json
import sqlite3

import pytest
import requests

import reaper_onboarding as setup
import server


class Reply:
    def __init__(self, payload, status=200):
        self.status_code, self.payload = status, payload
    def json(self):
        return self.payload


class GoogleFake:
    def __init__(self):
        self.files = {}
        self.tabs = {}
        self.headers = {}
        self.creates = 0
        self.fail_headers_once = False
        self.lost_create = False
        self.hide_search = False
        self.owned = True
        self.queries = []

    def __call__(self, method, url, **kwargs):
        assert kwargs['headers']['Authorization'].startswith('Bearer ')
        assert kwargs['allow_redirects'] is False
        self.queries.append((method, url))
        if url == setup.DRIVE:
            if method == 'GET':
                query = kwargs['params']['q']
                return Reply({'files': [] if self.hide_search else [{'id': key} for key, data in self.files.items()
                    if data['appProperties']['reaperOperation'] in query]})
            self.creates += 1
            key = 'workbook-' + str(self.creates)
            self.files[key] = kwargs['json']
            self.tabs[key] = {'Sheet1': 0}
            self.headers[key] = {}
            if self.lost_create:
                self.lost_create = False
                raise requests.Timeout()
            return Reply({'id': key})
        if url.startswith(setup.DRIVE + '/'):
            key = url.rsplit('/', 1)[1]
            return Reply({**self.files[key], 'ownedByMe': self.owned})
        suffix = url[len(setup.SHEETS) + 1:]
        key = suffix.split('/')[0].split(':')[0]
        if method == 'GET' and '/values/' not in suffix:
            return Reply({'sheets': [{'properties': {'title': t, 'sheetId': i}} for t, i in self.tabs[key].items()]})
        if '/values/' in suffix:
            cell_range = suffix.split('/values/', 1)[1]
            return Reply({'values': self.headers[key].get(cell_range, [])})
        if suffix.endswith('/values:batchUpdate'):
            if self.fail_headers_once:
                self.fail_headers_once = False
                raise requests.Timeout()
            for data in kwargs['json']['data']:
                self.headers[key][data['range']] = data['values']
            return Reply({})
        if suffix.endswith(':batchUpdate'):
            for command in kwargs['json']['requests']:
                if 'updateSheetProperties' in command:
                    p = command['updateSheetProperties']['properties']
                    old = next(t for t, i in self.tabs[key].items() if i == p['sheetId'])
                    del self.tabs[key][old]
                    self.tabs[key][p['title']] = p['sheetId']
                if 'addSheet' in command:
                    p = command['addSheet']['properties']
                    self.tabs[key][p['title']] = p['sheetId']
            return Reply({})
        raise AssertionError((method, url, kwargs))


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv('SAGEWIRE_REAPER_DB_PATH', str(tmp_path / 'registry.sqlite3'))
    monkeypatch.setenv('SAGEWIRE_SCOUT_TENANTS_JSON', '{"operations":[]}')
    google = GoogleFake()
    monkeypatch.setattr(setup.requests, 'request', google)
    def userinfo(url, headers, timeout):
        token = headers['Authorization'].split()[1]
        return Reply({'email': token + '@example.com', 'email_verified': True, 'sub': token})
    monkeypatch.setattr(server.requests, 'get', userinfo)
    server.app.config['TESTING'] = True
    return server.app.test_client(), google


def post(client, name='Test Operation', user='owner', **extra):
    return client.post('/reaper/api/v1/onboarding', json={'name': name, **extra},
                       headers={'Authorization': 'Bearer ' + user})


def config(client, user='owner'):
    return client.get('/reaper/api/v1/config', headers={'Authorization': 'Bearer ' + user})


def test_customer_owned_sheet_and_shared_config(environment):
    client, google = environment
    response = post(client)
    assert response.status_code == 200
    payload = response.json
    assert set(google.tabs['workbook-1']) == {'Animals', 'HeadCounter'}
    assert payload['operation']['role'] == 'owner'
    assert payload['features']['bovine_beacon'] is False
    assert google.headers['workbook-1']['Animals!A1:P1'] == [setup.ANIMALS_HEADERS]
    assert google.headers['workbook-1']['HeadCounter!A1:M1'] == [setup.SCAN_HEADERS]
    assert config(client).json == payload
    assert client.get('/scout/api/v1/config', headers={'Authorization': 'Bearer owner'}).json == payload
    assert client.get('/reaper/api/v1/onboarding', headers={'Authorization': 'Bearer owner'}).json == payload
    with setup.database() as db:
        assert 'Bearer' not in str([dict(r) for r in db.execute('SELECT * FROM operations')])


def test_retry_and_process_restart_reuses_workbook(environment):
    client, google = environment
    original = post(client).json
    assert post(server.app.test_client(), name='Ignored rename').json == original
    assert google.creates == 1


def test_headers_timeout_resumes_and_pending_cannot_scan(environment):
    client, google = environment
    google.fail_headers_once = True
    assert post(client).status_code == 503
    pending = config(client).json
    assert pending['status'] == 'ONBOARDING_REQUIRED'
    assert 'operation' not in pending
    assert pending['onboarding']['name'] == 'Test Operation'
    assert post(client).json['status'] == 'ACTIVE'
    assert google.creates == 1


def test_lost_create_recovers_by_private_property(environment):
    client, google = environment
    google.lost_create = True
    assert post(client).status_code == 503
    assert post(client).json['status'] == 'ACTIVE'
    assert google.creates == 1


def test_ambiguous_create_with_stale_search_never_creates_again(environment):
    client, google = environment
    google.lost_create = True
    assert post(client).status_code == 503
    google.hide_search = True
    retry = post(client)
    assert retry.status_code == 409
    assert retry.json['error']['code'] == 'CREATE_OUTCOME_UNKNOWN'
    assert google.creates == 1
    google.hide_search = False
    assert post(client).json['status'] == 'ACTIVE'


def test_two_accounts_are_isolated(environment):
    client, google = environment
    a, b = post(client).json, post(client, user='other').json
    assert a['operation']['sheet_id'] != b['operation']['sheet_id']
    assert config(client).json == a
    assert config(client, user='other').json == b
    assert google.creates == 2


def test_name_not_sheet_or_members_supplied_by_client(environment):
    client, google = environment
    assert post(client, sheet_id='someone-elses-sheet').status_code == 400
    assert google.creates == 0
    for name in ('', 'x' * 81, None, 23, 'bad\nname'):
        assert post(client, name=name).status_code == 400
    assert google.creates == 0


def test_wrong_owner_is_not_activated(environment):
    client, google = environment
    google.owned = False
    response = post(client)
    assert response.status_code == 409
    assert config(client).json['status'] == 'ONBOARDING_REQUIRED'
    google.owned = True
    assert post(client).json['status'] == 'ACTIVE'


def test_changed_headers_are_not_overwritten(environment):
    client, google = environment
    google.fail_headers_once = True
    post(client)
    google.headers['workbook-1']['Animals!A1:P1'] = [['Customer data']]
    response = post(client)
    assert response.status_code == 409
    assert google.headers['workbook-1']['Animals!A1:P1'] == [['Customer data']]
    assert config(client).json['status'] == 'ONBOARDING_REQUIRED'


def test_existing_or_disabled_static_members_cannot_create(environment, monkeypatch):
    client, google = environment
    for status in ('ACTIVE', 'DISABLED'):
        monkeypatch.setenv('SAGEWIRE_SCOUT_TENANTS_JSON', json.dumps({'operations': [{
            'id': 'existing', 'name': 'Existing', 'sheet_id': 'existing-sheet',
            'status': status, 'members': ['owner@example.com']}]}))
        assert post(client).status_code == 409
    assert google.creates == 0


def test_missing_storage_fails_before_any_google_side_effect(environment, monkeypatch):
    client, google = environment
    monkeypatch.delenv('SAGEWIRE_REAPER_DB_PATH')
    assert post(client).status_code == 503
    assert google.creates == 0


def test_no_bearer_or_invalid_registry_blocks_setup(environment, monkeypatch):
    client, google = environment
    assert client.post('/reaper/api/v1/onboarding', json={'name': 'Ranch'}).status_code == 401
    monkeypatch.setenv('SAGEWIRE_SCOUT_TENANTS_JSON', '[]')
    assert post(client).status_code == 503
    assert google.creates == 0


def test_cross_worker_lock_does_not_allow_second_create(environment):
    client, google = environment
    with open(setup.database_path() + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert post(client).json['error']['code'] == 'SETUP_BUSY'
    assert google.creates == 0


def test_same_email_different_subject_is_denied(environment):
    client, google = environment
    post(client)
    with pytest.raises(setup.SetupError, match='IDENTITY_CONFLICT'):
        setup.find_operation({'email': 'owner@example.com', 'sub': 'other-subject'})


def test_duplicate_recovery_matches_fail_closed(environment):
    client, google = environment
    google.lost_create = True
    post(client)
    google.files['duplicate'] = dict(google.files['workbook-1'])
    assert post(client).json['error']['code'] == 'DUPLICATE_WORKBOOKS'
    assert google.creates == 1
