# Vera Context Engine

A deterministic Python API for the magicpin challenge. It composes messages from the contexts pushed by the judge, without an external LLM or fabricated offers. The current trigger drives the message; merchant metrics and active offers provide the business facts, and category knowledge supplies relevant research and voice. Customer sends require a matching customer context and consent. SQLite stores context versions, suppression keys, conversation state, and opt-outs.

## Run

Requires Python 3.10 or newer; no packages to install.

```bash
python bot.py
```

The server binds `0.0.0.0:$PORT` (`8080` by default). Set `STATE_DB` to a writable SQLite path. Optional metadata settings: `TEAM_NAME`, comma-separated `TEAM_MEMBERS`, `CONTACT_EMAIL`. No API credentials are needed by the bot.

Endpoints: `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata`. Optional `POST /v1/teardown` clears judge state.

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python tests/test_api.py
python tests/run_judge_operational.py
```

The supplied `judge_simulator.py` needs an independent scoring LLM key. Its configuration reads `BOT_URL`, `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, and `TEST_SCENARIO` from the environment. For a scored run, start the API and set those variables before running `python judge_simulator.py`. The operational helper invokes the simulator's real warmup and conversation scenarios without assigning LLM scores.

## Deployment

`render.yaml` describes a Render Python web service with `python bot.py` as its start command and `/v1/healthz` as its health check. Connect this project as a Git repository to deploy its Blueprint. The free plan provides a stable URL but has an ephemeral filesystem and can spin down when idle. For an uninterrupted, durable stateful evaluation, configure a paid service with a persistent disk and set `STATE_DB` under its mount path, or use a durable external store. Keep a single server instance with this SQLite implementation.

Tradeoff: local rules avoid LLM latency and hallucination, but sparse triggers are suppressed, and message fluency is less varied than a high-quality language model. More verified customer consent scopes and live availability data would improve customer follow-ups.
