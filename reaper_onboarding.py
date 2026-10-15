"""Shared Reaper operation provisioning. No Google credentials are persisted.

SQLite plus an OS lock supports one host with multiple Gunicorn workers. The
configured database and its lock must live on the same persistent local volume.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import uuid

import requests

ANIMALS_HEADERS = ['Tag EPC', 'Animal ID', 'Printed Tag', 'Name', 'Species', 'Sex',
                   'Breed', 'DOB', 'Color', 'Description', 'Dam ID', 'Sire ID',
                   'Location', 'Status', 'Weight', 'Notes']
SCAN_HEADERS = ['HCID', 'Session', 'Date', 'Tag EPC', 'Pasture', 'First Seen',
                'Latitude', 'Longitude', 'GPS Accuracy (m)', 'Read Count',
                'Mode', 'Operation', 'Logged By']
DRIVE = 'https://www.googleapis.com/drive/v3/files'
SHEETS = 'https://sheets.googleapis.com/v4/spreadsheets'
MIME = 'application/vnd.google-apps.spreadsheet'
FEATURES = {'field_head_count': True, 'animal_lookup': True, 'bovine_beacon': False}


class SetupError(Exception):
    def __init__(self, code, message, status=503):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def database_path():
    value = os.getenv('SAGEWIRE_REAPER_DB_PATH', '')
    if not value or not os.path.isabs(value):
        raise SetupError('SETUP_NOT_CONFIGURED', 'Account setup is not available yet.')
    return value


@contextmanager
def database():
    path = database_path()
    db = None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS operations (
            subject TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            sheet_id TEXT, state TEXT NOT NULL DEFAULT 'NEW',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        db.commit()
        yield db
    except (OSError, sqlite3.Error):
        raise SetupError('SETUP_STORAGE_UNAVAILABLE', 'Account setup storage is unavailable.') from None
    finally:
        if db is not None:
            db.close()


def find_operation(identity):
    with database() as db:
        row = db.execute('SELECT * FROM operations WHERE subject = ? OR email = ?',
                         (identity.get('sub'), identity['email'])).fetchone()
        if row and (row['subject'] != identity.get('sub') or row['email'] != identity['email']):
            raise SetupError('IDENTITY_CONFLICT', 'Operation identity requires support review.', 409)
        return dict(row) if row else None


def public_config(row):
    if row['state'] != 'ACTIVE':
        return {'status': 'ONBOARDING_REQUIRED',
                'onboarding': {'name': row['name'], 'state': row['state']}}
    return {'status': 'ACTIVE', 'operation': {
        'id': row['id'], 'name': row['name'], 'sheet_id': row['sheet_id'],
        'schema_version': '1', 'role': 'owner'}, 'features': FEATURES.copy()}


def update(row, **changes):
    with database() as db:
        # Column names are internal constants, never request data.
        fields = ', '.join(key + ' = ?' for key in changes)
        db.execute('UPDATE operations SET ' + fields + ', updated_at = CURRENT_TIMESTAMP WHERE subject = ?',
                   (*changes.values(), row['subject']))
        db.commit()
    row.update(changes)


class Google:
    def __init__(self, token, timeout):
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        self.timeout = timeout

    def call(self, method, url, **kwargs):
        try:
            response = requests.request(method, url, headers=self.headers,
                                        timeout=self.timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SetupError('GOOGLE_UNAVAILABLE', 'Google could not be reached. Retry setup to resume.') from None
        if response.status_code == 401:
            raise SetupError('INVALID_GOOGLE_TOKEN', 'Please sign in to Google again.', 401)
        if response.status_code == 403:
            raise SetupError('GOOGLE_PERMISSION_REQUIRED', 'Google Drive and Sheets access is required. Sign in again and allow access.', 403)
        if not 200 <= response.status_code < 300:
            raise SetupError('GOOGLE_REQUEST_FAILED', 'Google could not finish setup. Retry to resume.', 502)
        try:
            return response.json()
        except ValueError:
            raise SetupError('GOOGLE_INVALID_RESPONSE', 'Google returned an incomplete response. Retry setup.') from None


def _verify_file(google, row):
    metadata = google.call('GET', DRIVE + '/' + row['sheet_id'],
                           params={'fields': 'id,mimeType,ownedByMe,trashed,appProperties'})
    if (metadata.get('mimeType') != MIME or metadata.get('ownedByMe') is not True
            or metadata.get('trashed') is True
            or metadata.get('appProperties', {}).get('reaperOperation') != row['id']):
        raise SetupError('WORKBOOK_OWNERSHIP_CONFLICT', 'The setup workbook could not be verified. Contact support.', 409)


def _find_created_file(google, row):
    result = google.call('GET', DRIVE, params={
        'q': "trashed = false and appProperties has { key='reaperOperation' and value='" + row['id'] + "' }",
        'spaces': 'drive', 'fields': 'files(id),nextPageToken', 'pageSize': 100})
    matches = result.get('files', [])
    if len(matches) > 1 or result.get('nextPageToken'):
        raise SetupError('DUPLICATE_WORKBOOKS', 'Multiple setup workbooks need support review.', 409)
    return matches[0]['id'] if matches else None


def _initialize_workbook(google, row):
    url = SHEETS + '/' + row['sheet_id']
    result = google.call('GET', url, params={'fields': 'sheets(properties(sheetId,title))'})
    tabs = {s['properties']['title']: s['properties']['sheetId'] for s in result.get('sheets', [])}
    commands = []
    # A Drive-created blank workbook may have Google's default Sheet1.
    # Reuse that tab without deleting data; unexpected headers below fail closed.
    if set(tabs) == {'Sheet1'}:
        tab_id = tabs.pop('Sheet1')
        tabs['Animals'] = tab_id
        commands.append({'updateSheetProperties': {
            'properties': {'sheetId': tab_id, 'title': 'Animals'}, 'fields': 'title'}})
    used_ids = set(tabs.values())
    for title in ('Animals', 'HeadCounter'):
        if title not in tabs:
            tab_id = next(i for i in range(1, len(used_ids) + 3) if i not in used_ids)
            used_ids.add(tab_id)
            tabs[title] = tab_id
            commands.append({'addSheet': {'properties': {'title': title, 'sheetId': tab_id}}})
    if commands:
        google.call('POST', url + ':batchUpdate', json={'requests': commands})
    expected = [('Animals!A1:P1', ANIMALS_HEADERS), ('HeadCounter!A1:M1', SCAN_HEADERS)]
    data = []
    for cell_range, headers in expected:
        values = google.call('GET', url + '/values/' + cell_range).get('values', [])
        if values and values != [headers]:
            raise SetupError('WORKBOOK_SCHEMA_CONFLICT', 'Setup found different headers. No existing headers were replaced.', 409)
        if not values:
            data.append({'range': cell_range, 'values': [headers]})
    if data:
        google.call('POST', url + '/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': data})
    # IDs remain text, including leading zeros. Formatting does not rewrite data.
    google.call('POST', url + ':batchUpdate', json={'requests': [
        {'repeatCell': {'range': {'sheetId': tabs['Animals'], 'startColumnIndex': 0, 'endColumnIndex': 16},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'TEXT'}}},
                        'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': {'sheetId': tabs['HeadCounter'], 'startColumnIndex': 3, 'endColumnIndex': 4},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'TEXT'}}},
                        'fields': 'userEnteredFormat.numberFormat'}}]})
    for cell_range, headers in expected:
        if google.call('GET', url + '/values/' + cell_range).get('values') != [headers]:
            raise SetupError('WORKBOOK_VERIFICATION_FAILED', 'Workbook verification is incomplete. Retry setup.')


def provision(identity, name, token, timeout):
    subject = identity.get('sub')
    if not isinstance(subject, str) or not subject.strip():
        raise SetupError('INVALID_GOOGLE_IDENTITY', 'A stable Google identity is required.', 403)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        raise SetupError('INVALID_OPERATION_NAME', 'Enter an operation name of 1–80 characters.', 400)
    # Serialize all writes across workers, with no long waiting HTTP requests.
    lock_path = database_path() + '.lock'
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = open(lock_path, 'a')
        os.chmod(lock_path, 0o600)
    except OSError:
        raise SetupError('SETUP_STORAGE_UNAVAILABLE', 'Account setup storage is unavailable.') from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('SETUP_BUSY', 'Another setup is finishing. Retry in a moment.', 409) from None
        row = find_operation(identity)
        if row is None:
            with database() as db:
                db.execute('INSERT INTO operations(subject,email,id,name) VALUES(?,?,?,?)',
                           (subject, identity['email'], 'reaper-' + uuid.uuid4().hex, name.strip()))
                db.commit()
            row = find_operation(identity)
        if row['state'] == 'ACTIVE':
            return public_config(row)
        google = Google(token, timeout)
        if not row['sheet_id']:
            recovered = _find_created_file(google, row)
            if recovered:
                update(row, sheet_id=recovered, state='INITIALIZING')
            elif row['state'] != 'NEW':
                # Drive may have accepted a create whose response was lost. A
                # second create is unsafe even if Drive search is not yet fresh.
                raise SetupError('CREATE_OUTCOME_UNKNOWN', 'Google has not confirmed the first workbook creation. Retry later; setup will not create a second workbook.', 409)
            else:
                update(row, state='CREATING')  # durable before the external side effect
                try:
                    created = google.call('POST', DRIVE, params={'fields': 'id'}, json={
                        'name': 'HerdMate — ' + row['name'], 'mimeType': MIME,
                        'appProperties': {'reaperOperation': row['id']}})
                except SetupError as error:
                    if error.status in (401, 403):
                        update(row, state='NEW')  # Google definitively rejected this create
                    raise
                if not isinstance(created.get('id'), str) or not created['id']:
                    raise SetupError('CREATE_OUTCOME_UNKNOWN', 'Google has not confirmed workbook creation. Retry later.', 409)
                update(row, sheet_id=created['id'], state='INITIALIZING')
        _verify_file(google, row)
        _initialize_workbook(google, row)
        _verify_file(google, row)
        update(row, state='ACTIVE')
        return public_config(row)
