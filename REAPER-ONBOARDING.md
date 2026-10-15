# Shared Reaper onboarding

Implementation candidate; not deployed or device accepted. Owner authorized
completion of shared Reaper-system onboarding on October 15, 2026.

## First delivery

A Google identity creates one operation with an owner role. The authenticated
customer's Google token creates a workbook in their own My Drive, with one
species-neutral Animals identity table and a HeadCounter event log. No service
account owns the workbook. The server verifies ownedByMe, the private creation
marker and both header rows before making the operation ACTIVE.

GET /reaper/api/v1/config returns only that verified identity's operation.
GET /reaper/api/v1/onboarding returns the same state.
POST /reaper/api/v1/onboarding accepts only {"name":"Operation name"}; Google
bearer authentication is required. Clients cannot supply an owner, Sheet ID,
operation ID, features or role. Scout's /scout/api/v1/config and onboarding
routes are compatibility aliases. Other Reaper clients can use these contracts;
their hardware adapters and interfaces are not implemented by this change.

Existing SAGEWIRE_SCOUT_TENANTS_JSON entries remain authoritative, including
DISABLED memberships. Existing DCC and demo workbooks are not changed, copied,
shared or migrated. Static members cannot create a parallel operation through
setup. The current one-operation resolution contract is retained; employee
invitations, multiple-operation switching and role management need later scope.
This is not a commercial seat limit.

## Persistence and recovery

Set SAGEWIRE_REAPER_DB_PATH to an absolute file path on persistent storage, for
example /data/reaper-onboarding.sqlite3. No default ephemeral database is used.
Mount /data before enabling setup, restrict it to the service, and back it up
with sqlite3's backup API while online (or copy it with the service stopped).
Keep the DB and its .lock on the same persistent LOCAL filesystem. One host /
one Gateway replica is supported; multiple Gunicorn workers are serialized with
an OS lock. Do not scale this deployment across hosts or use NFS without
replacing the store/lock with a transactional shared service.

The database contains only subject/email, operation ID/name, workbook ID and
provisioning state/timestamps; no access/refresh tokens or animal rows. Animal
headers pass through memory during verification. This is not a zero-retention
claim: identity/configuration metadata persists, and auth has its own credential
store. Logging, backups and browser caches have separate retention concerns.

NEW -> CREATING -> INITIALIZING -> ACTIVE. The server commits intent before
creating a file. The Drive create includes private appProperties.reaperOperation
in the same request. Repeated POSTs and process restarts use the saved ID or
recover the file by that marker. A lost response cannot automatically trigger
another create: if search has not found the original, CREATE_OUTCOME_UNKNOWN
requires retry later or support investigation. Google Workspace spreadsheets do
not support pre-generated create IDs; do not claim exactly-once external writes.
A crash immediately before the first create may also need support review.

Once the original is found, initialization resumes. Google's lone default Sheet1 is reused as Animals; other missing tabs are added.
Existing rows and other tabs are not deleted. Existing nonmatching headers block setup,
rather than overwriting customer data. IDs are formatted as text. Partial setups
never return an ACTIVE operation. Duplicate markers, ownership conflicts and
identity changes fail closed. Never delete the state row or reset CREATING to
NEW just to clear an error: that can create another workbook. Inspect the
customer's Drive and original operation marker with their authorization first.
Restoring an old/missing database can also lose recovery state; preserve backups.

Google requires the Drive API and Sheets API enabled for the OAuth project.
The current consent requests openid, userinfo.email, spreadsheets and drive.file.
Drive and Sheets access must be granted. New files are created without a shared
Drive parent; the customer must have My Drive storage and organization policy
permission. OAuth app verification/test-user restrictions still apply.

## Coordinated deployment

Prepare all three review branches before deploying:
1. reaper_scout_auth: back up/preserve existing SCOUT_AUTH_DB on a persistent
   volume, retain GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REDIRECT_URI.
   The redirect must remain https://scanner.herdmate.ag. The additive DB migration
   preserves refresh tokens and adds private device sessions. Never put secrets
   in GitHub. This service now rejects email-only refresh and requires PKCE.
2. sagewire-api-gateway: preserve SAGEWIRE_SCOUT_TENANTS_JSON, mount /data, set
   SAGEWIRE_REAPER_DB_PATH=/data/reaper-onboarding.sqlite3, then redeploy the
   Gateway. No manual registry entry is needed for newly onboarded customers.
   The Docker command allows 180 seconds for multi-call setup. Check the
   reverse proxy request timeout is long enough; shorter timeouts are safe to
   retry but may interrupt the UI while server work continues.
3. uhf_ear_tag_app: deploy the scanner resource serving scanner.herdmate.ag.
   Both index.html and gateway.html must be from this branch. The public root
   welcome/help pages may share the same source resource.

Coordinate auth and scanner publication in a short maintenance window. Existing
clients using email-only refresh must sign in once after the update. Deploying
the scanner alone cannot complete the new flow; deploying the auth repair alone
will require older clients to update before they can exchange/refresh tokens.
Never roll auth back to email-only credential access as a compatibility fix.

## Verification before acceptance

Use a separate, authorized Google test identity not assigned to DCC/demo. Start
at https://scanner.herdmate.ag/gateway.html with fresh site state; complete the
real Google redirect/consent, create a named operation, inspect the workbook in
that account's Drive and verify headers/owner. Retry/reload setup and confirm one
workbook. Restart/redeploy the Gateway while keeping /data and confirm routing
persists. Sign out/in and verify operation and private device session isolation.
Check the personal DCC account and business demo account retain their existing
operation and Sheet destinations; a disabled account must not self-provision.

Actual animal creation/tag assignment, editable management records, custom
reports and export templates are follow-on workflows, not claimed complete by
account provisioning. Existing scan/export behavior is retained. A known test
animal may be entered into the customer's Animals sheet to verify lookup and
scan-write destination; do not write fictional records into DCC. Repeat on C72;
R6 EPC decoding remains a separate unresolved adapter task. No new signed AAB
is produced by this web/backend change.

## Automated validation

Run pip install -r requirements.txt -r requirements-dev.txt and python -m pytest
-q. Tests use simulated Google responses and temporary SQLite databases. They
verify state recovery, timeout ambiguity, ownership/header guards, existing and
disabled memberships, two-account isolation, private identity binding and worker
locking. They do not demonstrate actual Google consent, live API permissions,
Coolify persistence, hardware scans or customer acceptance.

Primary API references:
- https://developers.google.com/workspace/drive/api/guides/create-file
- https://developers.google.com/workspace/drive/api/guides/search-files
- https://developers.google.com/workspace/drive/api/guides/properties
- https://developers.google.com/workspace/sheets/api/guides/batchupdate
