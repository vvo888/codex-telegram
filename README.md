# Codex Telegram Bridge

Standalone Telegram bridge for the local `codex` CLI.

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

## Deployment

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

## Telegram Commands

- `/help` - show help
- `/status` - current session id, active request, queue size
- `/sessions` - show last 7 Codex sessions on the host, with buttons for history and attach
- `/history` - show compact history for the currently attached session
- `/cancel` - clear queued requests and staged attachments
- `/new` - clear queue and start a fresh Codex session with the next user message

## Notes

- If the service restarts while a request is running, that active request is moved back to the front of the queue on startup.
- `codex` must be installed on the host and available at `CODEX_TELEGRAM_CLI_PATH`.
- One bot token should be polled by one running bridge instance at a time.
