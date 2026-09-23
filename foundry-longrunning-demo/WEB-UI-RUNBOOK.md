# Foundry Trip Advisor Web UI

## Access URL

`https://<container-app-fqdn>/`

## Implemented features

- Private Trip / Business Trip switching
- Model selection:
  - `gpt-5.6-sol`
  - `gpt-5.6-terra`
  - `gpt-5.6-luna`
- Public Web grounded search for:
  - flights
  - hotels
  - activities
  - local transportation and weather alternatives
- Family composition, dates, budget, interests, and free-form notes
- Built-in sample: Tokyo to Bangkok, family of four, three nights
- Japanese itinerary with indicative budget and citations
- Server-side parsing into summary, flights, hotels, daily itinerary, budget,
  and pre-booking checklist sections
- Card-based rendering with HTML comparison tables, clickable links, and a
  sticky section navigator
- Automatic continuation when the first model response reaches its output limit
- Stored background Responses with live SSE event display
- Actual `response.web_search_call.*` and `response.completed` event tracking
- Response ID, sequence number, query count, source count, reconnect count,
  event count, and phase metadata display
- Result / execution-review view toggle in the right pane
- Copyable technical event JSON for demonstrations
- Responsive Web UI hosted on Azure Container Apps

## Architecture

```text
Browser
  |
  | HTTPS
  v
Azure Container Apps
  FastAPI + static HTML/CSS/JavaScript
  System-assigned managed identity
  |
  | Microsoft Entra ID
  v
Azure OpenAI Responses API
  + web_search
  |
  +-- gpt-5.6-sol
  +-- gpt-5.6-terra
  +-- gpt-5.6-luna
```

The Web UI does not expose an API key. The Container App identity and hosted
agent instance identity have the `Cognitive Services OpenAI User` role on the
Foundry account.

## Azure resources

| Resource | Name |
|---|---|
| Resource group | `<resource-group>` |
| Foundry account | `<foundry-account>` |
| Foundry project | `<foundry-project>` |
| Container App | `<container-app>` |
| Container Apps environment | `<container-apps-environment>` |
| Container Registry | `<container-registry>` |
| Log Analytics workspace | `<log-analytics-workspace>` |

## Model deployments

| Deployment | Version | SKU | Capacity |
|---|---|---|---:|
| `gpt-5.6-sol` | `2026-07-09` | `DataZoneStandard` | 300 |
| `gpt-5.6-terra` | `2026-07-09` | `DataZoneStandard` | 300 |
| `gpt-5.6-luna` | `2026-07-09` | `DataZoneStandard` | 300 |

Capacity 300 is used because a gpt-5.6 Web Search request can reserve a large
context window. Capacity 10, 50, and 200 produced `429 rate_limit_exceeded`
for a complete trip-planning prompt.

## Validation

### Web endpoint

```powershell
$url = "https://<container-app-fqdn>"
Invoke-RestMethod "$url/healthz"
Invoke-RestMethod "$url/api/models"
```

### Local tests

```powershell
Set-Location .\webapp
python -m unittest -v test_webapp.py
```

### All-model Web Search probe

```powershell
$env:AZURE_OPENAI_ENDPOINT = azd env get-value AZURE_OPENAI_ENDPOINT
Set-Location .\webapp
python .\probe_foundry.py --out .\model-probe-results.json --delay 65
```

Validated results:

| Model | Result | Duration | Citations |
|---|---|---:|---:|
| `gpt-5.6-sol` | PASS | 58.41 s | 24 |
| `gpt-5.6-terra` | PASS | 50.20 s | 24 |
| `gpt-5.6-luna` | PASS | 39.80 s | 24 |

### Deployed family-trip request

The deployed Web UI successfully generated a Bangkok family trip containing:

- flight comparison
- hotel comparison
- activities and daily itinerary
- rest periods for children
- indicative budget
- 24 source links

The deployed `gpt-5.6-luna` request completed in 42.21 seconds.

The formatted itinerary UI was validated with all six required sections:

```text
summary, flights, hotels, itinerary, budget, checklist
```

## Live progress and execution review

The Web UI starts a Foundry stored background response and consumes the actual
streaming events in a server-side worker. It does not use a timer-based fake
progress display.

Displayed execution plan:

1. Accept trip conditions
2. Search the public Web
3. Compare candidates and generate the itinerary
4. Validate required sections and citations
5. Parse and render for the Web UI

The **実行レビュー** view displays:

- job ID and Foundry response ID
- response status
- total event count
- latest `sequence_number`
- Web Search query list
- source count
- reconnect count
- phase status summary
- up to 120 recent event records
- copyable JSON containing progress metadata and events

Deployed SSE validation:

| Check | Result |
|---|---|
| metadata event | PASS |
| progress events | PASS |
| result event | PASS |
| done event | PASS |
| Web Search event | PASS |
| `response.completed` | PASS |
| Required itinerary sections | PASS |
| Captured events | 175 |
| Search queries | 4 |
| Sources | 32 |
| Last sequence number | 174 |

## Security and operational notes

- The Web UI is publicly reachable for demonstration purposes.
- It limits processing to two concurrent requests and one Container App replica.
- No Azure credential or API key is sent to the browser.
- Web content is treated as untrusted data, not instructions.
- Prices, availability, schedules, and opening hours must be rechecked before booking.
- Foundry Web Search uses Grounding with Bing. The Microsoft Data Protection
  Addendum does not apply to data sent to this service, and data can flow
  outside compliance and geographic boundaries.
- Do not enter secrets, personal identifiers, passport data, or confidential
  business information into demo requests.

## Primary source files

- `webapp/main.py`
- `webapp/static/index.html`
- `webapp/static/app.js`
- `webapp/static/styles.css`
- `webapp/probe_foundry.py`
- `webapp/test_webapp.py`
