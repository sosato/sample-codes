# GPT-6 Astra Playwright computer-use demo

This sample connects a local Playwright browser to a GPT-6 Astra deployment on
Microsoft Foundry. The model returns `computer` actions, the Python process
executes those actions locally, and a screenshot is returned to the model after
each action group.

Configure the endpoint for your Foundry account:

```text
https://<account>.services.ai.azure.com/openai/v1/responses
```

Set `FOUNDRY_RESPONSES_ENDPOINT` or pass `--endpoint`. The deployment name
defaults to `gpt-6-astra`; if Foundry uses a different deployment name, set
`FOUNDRY_MODEL_DEPLOYMENT` or pass `--model`.

## Prerequisites

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/) or pip
- Access to the Foundry deployment
- One of:
  - Microsoft Entra ID authentication through `az login`
  - A resource key in `FOUNDRY_API_KEY`

For Entra ID, the signed-in identity needs permission to invoke the deployed
model, such as `Cognitive Services OpenAI User` on the Foundry resource.

## Setup with uv

```powershell
Set-Location C:\CLI\Git\sample-codes\gpt6-computeuse-demo
uv sync
uv run playwright install chromium
Copy-Item .env.example .env
```

If necessary, edit `.env` and change the deployment name. Do not commit `.env`.

If this workstation must use the Microsoft package proxy, run:

```powershell
uv sync --default-index https://packagefeedproxy.microsoft.io/pypi/simple/
```

Test the browser and bundled page without making an API call:

```powershell
uv run python demo.py --check --headless
```

## Interactive demo

```powershell
uv run python demo.py
```

After startup, enter any real website URL:

```text
URL> https://example.com
```

The browser remains open and waits for tasks:

```text
Task> Inspect the page and report the visible main heading.
```

After Astra completes the task, the same browser session returns to `Task>` so
that another instruction can be entered. Interactive commands:

```text
/open https://www.microsoft.com   Open another site and allow its origin
/url                              Show the current page URL
/help                             Show commands
/quit                             Close the browser and exit
```

Screenshots and `trace.json` are written under a new
`artifacts\<timestamp>\` directory for each task.

## Run the bundled local page

Open the included travel-request page and wait for an interactive task:

```powershell
uv run python demo.py --demo
```

Example task:

```text
Task> Set Destination to Seattle, Travelers to 3, Budget to 2500, Pace to Balanced, enable Breakfast and Airport transfer, then select Generate request and verify the result.
```

## One-shot execution

Pass both the site and instruction to run one task and exit:

```powershell
uv run python demo.py `
  --url "https://example.com" `
  --task "Inspect this page and report its visible heading. Do not navigate away."
```

Navigation is restricted to the initial origin. For a legitimate redirect or
secondary origin, explicitly add it:

```powershell
uv run python demo.py `
  --url "https://app.example.com" `
  --allow-origin "https://login.example.com" `
  --task "Open the settings page and report the visible section names."
```

Useful options:

```text
--model <deployment-name>
--headless
--slow-mo 300
--max-turns 20
--reasoning-effort low|medium|high|xhigh|max
--artifacts <directory>
--demo
```

## Authentication

By default, the sample uses `DefaultAzureCredential` and requests an access token
for `https://ai.azure.com/.default`. On a developer workstation, this normally
uses the current Azure CLI login:

```powershell
az login
az account show
```

To use a resource key instead:

```powershell
$env:FOUNDRY_API_KEY = "<resource-key>"
uv run python demo.py
```

## Safety boundaries

- Generated actions operate a real local browser.
- Use test pages and non-production accounts.
- The sample blocks navigation to origins not explicitly allowed.
- The model is instructed not to sign in, purchase, upload files, transmit
  secrets, or perform destructive actions.
- Page content is treated as untrusted and cannot expand the user's authority.
- Runs stop after 20 model turns by default.
- Inspect the final browser state and screenshots; a model message alone is not
  proof that the task succeeded.

The navigation guard and prompts are demonstration controls, not a hardened
sandbox. Use a disposable browser profile or VM before adapting this sample to
sensitive sites.

## Troubleshooting

**Model or deployment not found**

Pass the Foundry deployment name, not necessarily the catalog model ID:

```powershell
uv run python demo.py --model "<deployment-name>"
```

**Authentication failed**

Confirm `az account show` succeeds and the identity has model invocation
permission. Alternatively, set `FOUNDRY_API_KEY`.

**The endpoint rejects `{"type": "computer"}`**

The deployed model or Foundry endpoint does not expose the native computer tool.
Confirm that the deployment is GPT-6 Astra and that computer use is enabled for
the deployment/region. The error is intentionally surfaced rather than silently
falling back to a different capability.

**Chromium is missing**

```powershell
uv run playwright install chromium
```

**`uv sync` fails while connecting to `files.pythonhosted.org`**

Use the configured Microsoft package proxy:

```powershell
uv sync --default-index https://packagefeedproxy.microsoft.io/pypi/simple/
```
