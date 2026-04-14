# Codex Telegram Bridge

Standalone Telegram bridge for the local `codex` CLI.

Run Codex from Telegram, keep one persistent Codex session per Telegram chat, send screenshots and files as context, and point the bridge at any project on the server via `CODEX_TELEGRAM_WORKDIR`.

This repository is intentionally standalone: no dependency on `predictive-dialer`, no bundled production secrets, and no requirement to move existing Codex sessions from another host.

## Highlights

- one Telegram chat maps to one persistent Codex session
- supports text, images, and document attachments
- queues later messages while Codex is still busy
- can attach a chat to one of the latest local Codex sessions
- runs as a simple `systemd` service with a Python virtualenv

## What It Does

- accepts text messages from Telegram
- accepts `photo` and `document` attachments from Telegram
- keeps one persistent Codex session per Telegram `chat_id`
- sends new prompts via `codex exec` and continues context via `codex exec resume`
- relays intermediate Codex chat messages back to Telegram
- queues later Telegram messages while Codex is busy
- supports `/new`, `/status`, `/cancel`, `/sessions`, `/history`

## Important Behavior

- Authorization is restricted by `TELEGRAM_ALLOWED_USERNAMES` and/or `TELEGRAM_ALLOWED_CHAT_IDS`.
- By default the service only accepts private chats.
- Queueing is the default behavior.
- Immediate hard-interrupt of the current Codex turn is intentionally not implemented.
- Attachments without a caption are staged locally and injected into the next text message.
- Images are passed to Codex via `--image`; non-image documents are saved on disk and referenced by absolute path in the prompt.
- By default runtime files live under `/opt/codex-telegram/.codex-telegram-bot/`.

## Repository Layout

- service code: `app/services/codex_telegram_bridge.py`
- tests: `tests/test_codex_telegram_bridge.py`
- example env: `.env.example`
- systemd unit template: `infrastructure/codex-telegram-bot.service`
- runtime state directory: `.codex-telegram-bot/` after first start

## Quick Start

Recommended target paths:

- bridge repo: `/opt/codex-telegram`
- target project that Codex should operate in: for example `/opt/another-project`

Steps:

1. Clone the repo:

```bash
git clone git@github.com:vvo888/codex-telegram.git /opt/codex-telegram
cd /opt/codex-telegram
```

2. Create the virtualenv and install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

3. Create the env file:

```bash
cp .env.example .env
```

4. Edit `.env` and set at least:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERNAMES=your_username
CODEX_TELEGRAM_WORKDIR=/opt/another-project
```

5. Install the systemd unit:

```bash
cp infrastructure/codex-telegram-bot.service /etc/systemd/system/codex-telegram-bot.service
systemctl daemon-reload
systemctl enable --now codex-telegram-bot.service
```

6. Check logs:

```bash
journalctl -u codex-telegram-bot.service -f
```

7. Open Telegram and send `/start`.

## Telegram Commands

- `/help` - show help
- `/status` - current session id, active request, queue size
- `/sessions` - show last 7 Codex sessions on the host, with buttons for history and attach
- `/history` - show compact history for the currently attached session
- `/cancel` - clear queued requests and staged attachments
- `/new` - clear queue and start a fresh Codex session with the next user message

## Security

- No real Telegram tokens, API keys, or passwords are committed in this repository.
- `.env` is ignored by git and must be created locally from `.env.example`.
- The default setup enables `CODEX_TELEGRAM_DANGEROUS_BYPASS=true`, which gives Codex broad host access. Treat the bot token like privileged access.
- One bot token should be polled by one running bridge instance at a time.

## Notes

- If the service restarts while a request is running, that active request is moved back to the front of the queue on startup.
- `codex` must be installed on the host and available at `CODEX_TELEGRAM_CLI_PATH`.
- The bridge stores staged uploads and queue state under `/opt/codex-telegram/.codex-telegram-bot/` by default.
