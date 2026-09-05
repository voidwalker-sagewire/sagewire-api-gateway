# SageWire API Gateway

The SageWire API Gateway is the common entry point for SageWire services.

**Version:** `1.1.0`

Runs on port **5010**.

---

## Gateway endpoints

```text
GET  /health
GET  /services

Existing service routes

POST /tts/speak
POST /stt/transcribe
POST /loc/stamp

Onboarding routes

GET /onboarding
GET /onboarding/health
GET /onboarding/ready

The SageWire Onboarding API is exposed through:

/onboarding/api/v1/...

Supported methods:

GET
POST
PUT
PATCH
DELETE

For example:

GET  /onboarding/api/v1/flows
POST /onboarding/api/v1/sessions
GET  /onboarding/api/v1/events

Requests are forwarded only to the registered SageWire Onboarding Service.

The Gateway is not an open proxy.


---

Registered backend services

tts         https://tts.sagewire.dev
stt         https://stt.sagewire.dev
loc         https://loc.sagewire.dev
wx          https://weather.herdmate.ag
onboarding  https://onboarding.sagewire.dev

Backend URLs may be overridden with environment variables:

SAGEWIRE_TTS_URL
SAGEWIRE_STT_URL
SAGEWIRE_LOC_URL
SAGEWIRE_WEATHER_URL
SAGEWIRE_ONBOARDING_URL

Gateway request timeout may be configured with:

SAGEWIRE_GATEWAY_TIMEOUT

Default timeout:

20 seconds


---

Error behavior

If a registered upstream service cannot be reached, the Gateway returns:

502 Bad Gateway

If an upstream request times out, the Gateway returns:

504 Gateway Timeout


---

Architecture

Application
    |
    v
SageWire API Gateway
    |
    +-- EmberVox TTS
    +-- SageWire STT
    +-- SageWire Location
    +-- HerdMate Weather
    +-- SageWire Onboarding

Applications should depend on the SageWire Gateway rather than directly coupling themselves to individual service deployment locations.

A product may depend on a service.

A service must not depend on a product
