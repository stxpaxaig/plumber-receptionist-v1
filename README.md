# Plumber Receptionist V1

An owned-code missed-call plumbing receptionist. A SIP carrier sends the real telephone call to OpenAI Realtime SIP. This server verifies OpenAI's incoming-call webhook, accepts the call with our private instructions, monitors the call over a sideband WebSocket, executes the lead-saving tool, and stores the structured lead and transcript in SQLite.

## What this V1 does

- Answers an incoming SIP telephone call with `gpt-realtime-2.1`
- Supports natural interruption through semantic voice activity detection
- Collects and confirms name, callback number, service address, issue, and urgency
- Prohibits invented pricing, availability, promises, and diagnoses
- Saves structured leads, call time, summary, caller metadata, and transcript
- Verifies OpenAI webhook signatures
- Exposes saved leads only through a bearer-token-protected JSON endpoint

## Required environment variables

Copy `.env.example` to `.env` for local development. Never commit real secrets.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Project API key used to accept and monitor calls |
| `OPENAI_WEBHOOK_SECRET` | Signing secret supplied when the OpenAI webhook is created |
| `LEADS_ADMIN_TOKEN` | Long random token protecting `/api/leads` |
| `COMPANY_NAME` | Spoken plumbing company name |
| `DATABASE_PATH` | SQLite path; mount persistent storage here in deployment |

## Local verification

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 0 app:app
```

Check `GET /health`. A real incoming-call webhook cannot be tested locally without a public HTTPS address and valid OpenAI webhook signature.

## Deployment contract

- Start command: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 app:app`
- Health endpoint: `/health`
- OpenAI webhook endpoint: `/webhooks/openai`
- Mount a persistent disk and set `DATABASE_PATH` to a path on that disk.
- Use one process for V1 because active call controllers run in-process.

## Telephone route

The SIP carrier destination will be:

```text
sip:YOUR_OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls
```

OpenAI sends `realtime.call.incoming` to this server. The server accepts it and opens the private sideband control connection. Do not point a number to the SIP destination until the public server and webhook are configured.

## Retrieve leads

```bash
curl -H "Authorization: Bearer $LEADS_ADMIN_TOKEN" https://YOUR_SERVER/api/leads
curl -H "Authorization: Bearer $LEADS_ADMIN_TOKEN" https://YOUR_SERVER/api/leads/1
```

The detail response includes the complete transcript.
