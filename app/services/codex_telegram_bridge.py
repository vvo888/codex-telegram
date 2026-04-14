from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx


SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
STREAM_READ_CHUNK_BYTES = 16 * 1024
MEDIA_GROUP_FLUSH_SECONDS = 1.0
BRIDGE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_STATE_DIR = BRIDGE_ROOT / ".codex-telegram-bot"


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def parse_allowed_usernames(raw_value: str) -> set[str]:
    usernames: set[str] = set()
    for item in raw_value.split(","):
        normalized = normalize_username(item)
        if normalized:
            usernames.add(normalized)
    return usernames


def parse_allowed_chat_ids(raw_value: str) -> set[int]:
    chat_ids: set[int] = set()
    for item in raw_value.split(","):
        chunk = item.strip()
        if not chunk:
            continue
        chat_ids.add(int(chunk))
    return chat_ids


def split_telegram_text(text: str, limit: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []

    chunks: list[str] = []
    remaining = normalized
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < int(limit * 0.6):
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def sanitize_filename(name: str, fallback: str = "attachment") -> str:
    candidate = Path(name).name.strip().replace(" ", "_")
    if not candidate:
        candidate = fallback
    sanitized = FILENAME_SANITIZE_RE.sub("_", candidate)
    sanitized = sanitized.strip("._-")
    return sanitized or fallback


def is_supported_image(mime_type: str | None, original_name: str | None, telegram_path: str | None) -> bool:
    if mime_type:
        normalized_mime = mime_type.lower()
        if normalized_mime in {"image/jpeg", "image/png", "image/webp"}:
            return True
        if normalized_mime.startswith("image/"):
            return False

    candidates = [original_name, telegram_path]
    for candidate in candidates:
        if not candidate:
            continue
        suffix = Path(candidate).suffix.lower()
        if suffix in SAFE_IMAGE_EXTENSIONS:
            return True
    return False


def build_thread_name(prompt: str, limit: int = 72) -> str:
    normalized = " ".join(prompt.strip().split())
    if not normalized:
        return "Telegram bridge session"
    if len(normalized) <= limit:
        return normalized
    truncated_limit = max(limit - 3, 1)
    return normalized[:truncated_limit].rstrip() + "..."


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def extract_message_text(content: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for item in content or []:
        if item.get("type") in {"input_text", "output_text"}:
            text = str(item.get("text", ""))
            if text:
                parts.append(text)
    return "".join(parts).strip()


def shorten_preview(text: str, limit: int = 220) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 1)].rstrip() + "..."


@dataclass(slots=True)
class BridgeConfig:
    telegram_bot_token: str
    allowed_usernames: set[str]
    allowed_chat_ids: set[int]
    private_only: bool
    poll_timeout_seconds: int
    telegram_api_base: str
    workdir: str
    cli_path: str
    dangerous_bypass: bool
    skip_git_repo_check: bool
    profile: str | None
    model: str | None
    state_file: Path
    session_index_file: Path
    uploads_dir: Path
    max_attachment_bytes: int
    max_message_chars: int
    heartbeat_interval_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> BridgeConfig:
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        allowed_usernames = parse_allowed_usernames(os.getenv("TELEGRAM_ALLOWED_USERNAMES", ""))
        allowed_chat_ids = parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        if not allowed_usernames and not allowed_chat_ids:
            raise RuntimeError("Configure TELEGRAM_ALLOWED_USERNAMES or TELEGRAM_ALLOWED_CHAT_IDS")

        return cls(
            telegram_bot_token=telegram_bot_token,
            allowed_usernames=allowed_usernames,
            allowed_chat_ids=allowed_chat_ids,
            private_only=_env_bool("TELEGRAM_PRIVATE_ONLY", True),
            poll_timeout_seconds=max(int(os.getenv("CODEX_TELEGRAM_POLL_TIMEOUT_SECONDS", "50")), 1),
            telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            workdir=os.getenv("CODEX_TELEGRAM_WORKDIR", str(BRIDGE_ROOT)),
            cli_path=os.getenv("CODEX_TELEGRAM_CLI_PATH", "/usr/bin/codex"),
            dangerous_bypass=_env_bool("CODEX_TELEGRAM_DANGEROUS_BYPASS", True),
            skip_git_repo_check=_env_bool("CODEX_TELEGRAM_SKIP_GIT_REPO_CHECK", True),
            profile=(os.getenv("CODEX_TELEGRAM_PROFILE", "").strip() or None),
            model=(os.getenv("CODEX_TELEGRAM_MODEL", "").strip() or None),
            state_file=Path(
                os.getenv(
                    "CODEX_TELEGRAM_STATE_FILE",
                    str(BRIDGE_STATE_DIR / "state.json"),
                )
            ),
            session_index_file=Path(
                os.getenv(
                    "CODEX_TELEGRAM_SESSION_INDEX_FILE",
                    str(Path.home() / ".codex" / "session_index.jsonl"),
                )
            ),
            uploads_dir=Path(
                os.getenv(
                    "CODEX_TELEGRAM_UPLOADS_DIR",
                    str(BRIDGE_STATE_DIR / "uploads"),
                )
            ),
            max_attachment_bytes=max(
                int(os.getenv("CODEX_TELEGRAM_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024))),
                1024,
            ),
            max_message_chars=max(int(os.getenv("CODEX_TELEGRAM_MAX_MESSAGE_CHARS", "3500")), 512),
            heartbeat_interval_seconds=max(
                int(os.getenv("CODEX_TELEGRAM_HEARTBEAT_INTERVAL_SECONDS", "30")),
                10,
            ),
            log_level=os.getenv("CODEX_TELEGRAM_LOG_LEVEL", "INFO"),
        )


def build_codex_command(
    config: BridgeConfig,
    thread_id: str | None,
    prompt: str,
    image_paths: list[str] | None = None,
) -> list[str]:
    command = [config.cli_path]
    if config.profile:
        command.extend(["-p", config.profile])
    if thread_id:
        command.extend(["exec", "resume", "--json"])
    else:
        command.extend(["exec", "--json"])

    if config.dangerous_bypass:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if config.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if config.model:
        command.extend(["-m", config.model])
    if config.workdir and not thread_id:
        command.extend(["-C", config.workdir])
    for image_path in image_paths or []:
        command.extend(["--image", image_path])
    command.append("--")
    if thread_id:
        command.append(thread_id)
    command.append(prompt)
    return command


def summarize_codex_error(stderr_lines: list[str], returncode: int) -> str:
    stderr_tail = "\n".join(stderr_lines[-8:]).strip()
    lowered = stderr_tail.lower()

    if "403 forbidden" in lowered and "chatgpt.com" in lowered:
        return (
            "Codex на сервере сейчас не смог подключиться к OpenAI (`403 Forbidden`). "
            "Обычно это проблема сетевого маршрута, прокси или серверной авторизации Codex."
        )

    if stderr_tail:
        return f"Codex завершился с кодом {returncode}.\n{stderr_tail[:1200]}"

    return f"Codex завершился с кодом {returncode}."


@dataclass(slots=True)
class StoredAttachment:
    kind: str
    local_path: str
    original_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "local_path": self.local_path,
            "original_name": self.original_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StoredAttachment:
        return cls(
            kind=str(payload["kind"]),
            local_path=str(payload["local_path"]),
            original_name=payload.get("original_name"),
            mime_type=payload.get("mime_type"),
            size_bytes=payload.get("size_bytes"),
        )

    @property
    def display_name(self) -> str:
        return self.original_name or Path(self.local_path).name

    @property
    def is_image(self) -> bool:
        return self.kind == "image"


@dataclass(slots=True)
class CodexSessionSummary:
    session_id: str
    thread_name: str
    updated_at: str


@dataclass(slots=True)
class SessionVisibleMessage:
    role: str
    text: str
    timestamp: str | None = None
    phase: str | None = None

    @property
    def is_final_answer(self) -> bool:
        return self.role == "assistant" and self.phase == "final_answer"


def build_prompt_with_attachments(text: str, attachments: list[StoredAttachment]) -> str:
    prompt = text.strip() or "Используй приложенные материалы как контекст текущей сессии."
    if not attachments:
        return prompt

    lines = [prompt, "", "Материалы из Telegram уже сохранены на сервере:"]
    for attachment in attachments:
        details = [attachment.kind, attachment.local_path]
        if attachment.original_name:
            details.append(f"name={attachment.original_name}")
        if attachment.mime_type:
            details.append(f"mime={attachment.mime_type}")
        lines.append(f"- {'; '.join(details)}")
    return "\n".join(lines)


@dataclass(slots=True)
class PendingRequest:
    request_id: str
    text: str
    created_at: float
    source_message_id: int | None = None
    attachments: list[StoredAttachment] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        text: str,
        source_message_id: int | None = None,
        attachments: list[StoredAttachment] | None = None,
    ) -> PendingRequest:
        return cls(
            request_id=uuid.uuid4().hex,
            text=text,
            created_at=time.time(),
            source_message_id=source_message_id,
            attachments=list(attachments or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "text": self.text,
            "created_at": self.created_at,
            "source_message_id": self.source_message_id,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PendingRequest:
        return cls(
            request_id=str(payload["request_id"]),
            text=str(payload["text"]),
            created_at=float(payload.get("created_at", time.time())),
            source_message_id=payload.get("source_message_id"),
            attachments=[
                StoredAttachment.from_dict(item) for item in payload.get("attachments", [])
            ],
        )


@dataclass(slots=True)
class BufferedMediaGroup:
    chat_id: int
    media_group_id: str
    attachments: list[StoredAttachment] = field(default_factory=list)
    text: str = ""
    source_message_id: int | None = None
    flush_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ChatState:
    chat_id: int
    thread_id: str | None = None
    thread_name: str | None = None
    pending_attachments: list[StoredAttachment] = field(default_factory=list)
    queue: list[PendingRequest] = field(default_factory=list)
    active_request: PendingRequest | None = None
    active_started_at: float | None = None
    reset_session_after_current: bool = False
    last_username: str | None = None
    last_seen_at: float | None = None
    last_error: str | None = None
    worker_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)
    active_process: asyncio.subprocess.Process | None = field(default=None, repr=False, compare=False)
    last_codex_event_at: float | None = field(default=None, repr=False, compare=False)
    last_heartbeat_at: float | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "thread_name": self.thread_name,
            "pending_attachments": [attachment.to_dict() for attachment in self.pending_attachments],
            "queue": [item.to_dict() for item in self.queue],
            "active_request": self.active_request.to_dict() if self.active_request else None,
            "active_started_at": self.active_started_at,
            "reset_session_after_current": self.reset_session_after_current,
            "last_username": self.last_username,
            "last_seen_at": self.last_seen_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChatState:
        queue = [PendingRequest.from_dict(item) for item in payload.get("queue", [])]
        active_request_payload = payload.get("active_request")
        active_request = PendingRequest.from_dict(active_request_payload) if active_request_payload else None
        if active_request is not None:
            queue.insert(0, active_request)
            active_request = None
        return cls(
            chat_id=int(payload["chat_id"]),
            thread_id=payload.get("thread_id"),
            thread_name=payload.get("thread_name"),
            pending_attachments=[
                StoredAttachment.from_dict(item) for item in payload.get("pending_attachments", [])
            ],
            queue=queue,
            active_request=active_request,
            active_started_at=None,
            reset_session_after_current=bool(payload.get("reset_session_after_current", False)),
            last_username=payload.get("last_username"),
            last_seen_at=payload.get("last_seen_at"),
            last_error=payload.get("last_error"),
        )


class BridgeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[int, ChatState]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        chats: dict[int, ChatState] = {}
        for raw_chat_id, chat_payload in payload.get("chats", {}).items():
            chat_payload["chat_id"] = int(raw_chat_id)
            state = ChatState.from_dict(chat_payload)
            chats[state.chat_id] = state
        return chats

    def save(self, chats: dict[int, ChatState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "chats": {str(chat_id): state.to_dict() for chat_id, state in chats.items()},
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)


class CodexSessionIndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_recent(self, limit: int = 7) -> list[CodexSessionSummary]:
        entries: list[CodexSessionSummary] = []
        if not self.path.exists():
            return entries
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(payload.get("id", "")).strip()
            if not session_id:
                continue
            entries.append(
                CodexSessionSummary(
                    session_id=session_id,
                    thread_name=str(payload.get("thread_name") or session_id),
                    updated_at=str(payload.get("updated_at") or ""),
                )
            )
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return entries[:limit]

    def get(self, session_id: str) -> CodexSessionSummary | None:
        if not self.path.exists():
            return None
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != session_id:
                continue
            return CodexSessionSummary(
                session_id=session_id,
                thread_name=str(payload.get("thread_name") or session_id),
                updated_at=str(payload.get("updated_at") or ""),
            )
        return None

    def upsert(self, session_id: str, thread_name: str) -> None:
        entries: list[dict[str, Any]] = []
        if self.path.exists():
            for raw_line in self.path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") == session_id:
                    continue
                entries.append(payload)

        entries.append(
            {
                "id": session_id,
                "thread_name": thread_name,
                "updated_at": utc_now_iso(),
            }
        )
        entries.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in entries]
        tmp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp_path.replace(self.path)


class CodexSessionHistoryStore:
    def __init__(self, sessions_root: Path) -> None:
        self.sessions_root = sessions_root

    def find_session_file(self, session_id: str) -> Path | None:
        if not self.sessions_root.exists():
            return None
        candidates = sorted(self.sessions_root.rglob(f"*{session_id}.jsonl"), reverse=True)
        return candidates[0] if candidates else None

    def load_visible_messages(self, session_id: str) -> list[SessionVisibleMessage]:
        session_file = self.find_session_file(session_id)
        if session_file is None:
            raise FileNotFoundError(f"Session file not found for {session_id}")

        messages: list[SessionVisibleMessage] = []
        for raw_line in session_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "response_item":
                continue
            item = payload.get("payload") or {}
            if item.get("type") != "message":
                continue
            role = str(item.get("role") or "")
            phase = item.get("phase")
            text = extract_message_text(item.get("content"))
            if not text:
                continue
            if role == "assistant" and phase == "commentary":
                continue
            if role not in {"assistant", "user"}:
                continue
            messages.append(
                SessionVisibleMessage(
                    role=role,
                    text=text,
                    timestamp=payload.get("timestamp"),
                    phase=phase,
                )
            )
        return messages


class TelegramCodexBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.state_store = BridgeStateStore(config.state_file)
        self.session_index_store = CodexSessionIndexStore(config.session_index_file)
        self.session_history_store = CodexSessionHistoryStore(config.session_index_file.parent / "sessions")
        self.chats = self.state_store.load()
        self.state_lock = asyncio.Lock()
        self.update_offset = 0
        self.client: httpx.AsyncClient | None = None
        self.logger = logging.getLogger("codex-telegram-bridge")
        self.media_groups: dict[tuple[int, str], BufferedMediaGroup] = {}

    async def run(self) -> None:
        timeout = httpx.Timeout(connect=10.0, read=self.config.poll_timeout_seconds + 10.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            self.client = client
            await self._telegram_api("deleteWebhook", {"drop_pending_updates": False})
            async with self.state_lock:
                for state in self.chats.values():
                    self._upsert_session_index_locked(state)
                    if state.queue:
                        self._ensure_worker(state)
            backoff_seconds = 2.0
            try:
                while True:
                    try:
                        updates = await self._telegram_api(
                            "getUpdates",
                            {
                                "offset": self.update_offset,
                                "timeout": self.config.poll_timeout_seconds,
                                "allowed_updates": ["message", "callback_query"],
                            },
                        )
                        backoff_seconds = 2.0
                        for update in updates:
                            self.update_offset = max(self.update_offset, int(update["update_id"]) + 1)
                            await self._handle_update(update)
                    except Exception:
                        self.logger.exception("Telegram polling failed")
                        await asyncio.sleep(backoff_seconds)
                        backoff_seconds = min(backoff_seconds * 2, 30.0)
            finally:
                await self._shutdown()

    async def _shutdown(self) -> None:
        async with self.state_lock:
            states = list(self.chats.values())
            media_group_tasks = [
                group.flush_task
                for group in self.media_groups.values()
                if group.flush_task is not None and not group.flush_task.done()
            ]
        for state in states:
            if state.worker_task and not state.worker_task.done():
                state.worker_task.cancel()
        for task in media_group_tasks:
            task.cancel()
        for state in states:
            if state.active_process and state.active_process.returncode is None:
                state.active_process.terminate()
                try:
                    await asyncio.wait_for(state.active_process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    state.active_process.kill()
        await asyncio.gather(
            *[state.worker_task for state in states if state.worker_task],
            return_exceptions=True,
        )

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if message is None:
            callback_query = update.get("callback_query")
            if callback_query is None:
                return
            await self._handle_callback_query(callback_query)
            return
        await self._handle_message(message)

    def _is_authorized(self, chat: dict[str, Any], from_user: dict[str, Any] | None) -> bool:
        if self.config.private_only and chat.get("type") != "private":
            return False
        chat_id = int(chat["id"])
        if chat_id in self.config.allowed_chat_ids:
            return True
        if not from_user:
            return False
        username = normalize_username(from_user.get("username", ""))
        return bool(username) and username in self.config.allowed_usernames

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        from_user = callback_query.get("from") or {}
        callback_id = str(callback_query.get("id") or "")
        chat_id = int(chat.get("id", 0))
        if not callback_id or not chat_id:
            return

        if not self._is_authorized(chat, from_user):
            await self._answer_callback_query(callback_id, "Доступ запрещен.", show_alert=True)
            return

        data = str(callback_query.get("data") or "").strip()
        if not data:
            await self._answer_callback_query(callback_id)
            return

        try:
            if data == "sessions:list":
                await self._show_sessions_list(chat_id, edit_message=message)
                await self._answer_callback_query(callback_id)
                return

            if data.startswith("sessions:use:"):
                session_id = data.removeprefix("sessions:use:")
                await self._attach_session(chat_id, session_id)
                await self._answer_callback_query(callback_id, "Сессия подключена.")
                return

            if data.startswith("sessions:history:"):
                session_id = data.removeprefix("sessions:history:")
                await self._show_session_history(chat_id, session_id, offset=0)
                await self._answer_callback_query(callback_id)
                return

            if data.startswith("history:page:"):
                _, _, session_id, offset_text = data.split(":", 3)
                offset = max(int(offset_text), 0)
                await self._show_session_history_page(chat_id, session_id, offset, message)
                await self._answer_callback_query(callback_id)
                return

            if data.startswith("history:use:"):
                session_id = data.removeprefix("history:use:")
                await self._attach_session(chat_id, session_id)
                await self._answer_callback_query(callback_id, "Сессия подключена.")
                return

            await self._answer_callback_query(callback_id, "Неизвестное действие.", show_alert=True)
        except Exception as exc:
            self.logger.exception("Callback handler failed for chat_id=%s data=%s", chat_id, data)
            await self._answer_callback_query(callback_id, f"Ошибка: {exc}", show_alert=True)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = int(chat.get("id", 0))
        if not chat_id:
            return

        if not self._is_authorized(chat, from_user):
            self.logger.warning(
                "Rejected Telegram update from chat_id=%s username=%s",
                chat_id,
                from_user.get("username"),
            )
            if chat.get("type") == "private":
                await self._send_text(chat_id, "Доступ запрещен.")
            return

        # Telegram private topics emit service updates like `forum_topic_created`
        # without user text. They should be ignored, not treated as bad input.
        if message.get("forum_topic_created"):
            self.logger.info("Ignoring forum_topic_created service message for chat_id=%s", chat_id)
            return

        username = from_user.get("username")
        state = await self._get_or_create_state(chat_id, username)
        try:
            attachments = await self._download_message_attachments(chat_id, message)
        except Exception as exc:
            self.logger.exception("Failed to download Telegram attachment for chat_id=%s", chat_id)
            await self._send_text(chat_id, f"Не удалось скачать вложение: {exc}")
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        media_group_id = str(message.get("media_group_id") or "").strip()

        if media_group_id and attachments:
            await self._buffer_media_group_message(
                state=state,
                media_group_id=media_group_id,
                text=text,
                attachments=attachments,
                source_message_id=message.get("message_id"),
            )
            return

        if text.startswith("/") and not attachments:
            await self._handle_command(state, text)
            return

        await self._flush_media_groups_for_chat(chat_id)
        await self._process_incoming_payload(
            state=state,
            text=text,
            attachments=attachments,
            source_message_id=message.get("message_id"),
        )

    async def _process_incoming_payload(
        self,
        state: ChatState,
        text: str,
        attachments: list[StoredAttachment],
        source_message_id: int | None,
    ) -> None:
        chat_id = state.chat_id
        normalized_text = text.strip()

        if not normalized_text and not attachments:
            await self._send_text(chat_id, "Поддерживаются текст, фото и документы.")
            return

        if attachments and not normalized_text:
            async with self.state_lock:
                state.pending_attachments.extend(attachments)
                pending_count = len(state.pending_attachments)
                self._persist_locked()
            await self._send_text(
                chat_id,
                (
                    "Материалы из Telegram сохранены. Добавлю их в следующий текстовый запрос к Codex."
                    f"\nОжидают вложений: {pending_count}."
                ),
            )
            return

        async with self.state_lock:
            combined_attachments = [*state.pending_attachments, *attachments]
            state.pending_attachments.clear()
            pending_request = PendingRequest.create(
                text=normalized_text,
                source_message_id=source_message_id,
                attachments=combined_attachments,
            )
            was_busy = state.active_request is not None or bool(state.queue)
            state.queue.append(pending_request)
            position = len(state.queue) + (1 if state.active_request else 0)
            self._persist_locked()

        attachment_notice = ""
        if combined_attachments:
            attachment_notice = f" Вложений в запросе: {len(combined_attachments)}."
        if was_busy:
            await self._send_text(
                chat_id,
                f"Сообщение принято. Поставил в очередь: #{position}.{attachment_notice}",
            )
        else:
            await self._send_text(chat_id, f"Сообщение принято. Передаю в Codex.{attachment_notice}")
        async with self.state_lock:
            self._ensure_worker(state)

    async def _buffer_media_group_message(
        self,
        state: ChatState,
        media_group_id: str,
        text: str,
        attachments: list[StoredAttachment],
        source_message_id: int | None,
    ) -> None:
        async with self.state_lock:
            key = (state.chat_id, media_group_id)
            buffer = self.media_groups.get(key)
            if buffer is None:
                buffer = BufferedMediaGroup(chat_id=state.chat_id, media_group_id=media_group_id)
                self.media_groups[key] = buffer
            buffer.attachments.extend(attachments)
            if text and not buffer.text:
                buffer.text = text.strip()
            if buffer.source_message_id is None:
                buffer.source_message_id = source_message_id
            if buffer.flush_task and not buffer.flush_task.done():
                buffer.flush_task.cancel()
            buffer.flush_task = asyncio.create_task(
                self._flush_media_group_after_delay(state.chat_id, media_group_id)
            )

    async def _flush_media_group_after_delay(self, chat_id: int, media_group_id: str) -> None:
        try:
            await asyncio.sleep(MEDIA_GROUP_FLUSH_SECONDS)
            await self._flush_media_group(chat_id, media_group_id)
        except asyncio.CancelledError:
            return

    async def _flush_media_groups_for_chat(self, chat_id: int) -> None:
        async with self.state_lock:
            group_ids = [
                media_group_id
                for buffered_chat_id, media_group_id in self.media_groups.keys()
                if buffered_chat_id == chat_id
            ]
        for media_group_id in group_ids:
            await self._flush_media_group(chat_id, media_group_id)

    async def _flush_media_group(self, chat_id: int, media_group_id: str) -> None:
        async with self.state_lock:
            buffer = self.media_groups.pop((chat_id, media_group_id), None)
            state = self.chats.get(chat_id)
            if state is None:
                state = ChatState(chat_id=chat_id)
                self.chats[chat_id] = state
                self._persist_locked()
        if buffer is None:
            return
        await self._process_incoming_payload(
            state=state,
            text=buffer.text,
            attachments=buffer.attachments,
            source_message_id=buffer.source_message_id,
        )

    async def _handle_command(self, state: ChatState, raw_text: str) -> None:
        chat_id = state.chat_id
        parts = raw_text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()

        if command in {"/start", "/help"}:
            await self._send_text(
                chat_id,
                "\n".join(
                    [
                        "Codex Telegram bridge готов.",
                        "",
                        "Можно отправлять текст, фото и документы.",
                        "Фото/скрины лучше слать как Document, если важно не терять качество.",
                        "Вложение без подписи будет добавлено в следующий текстовый запрос.",
                        "",
                        "Команды:",
                        "/status - текущая сессия и очередь",
                        "/sessions - показать последние 7 Codex-сессий и подключиться к одной из них",
                        "/history - показать историю текущей привязанной сессии",
                        "/new - очистить очередь и начать новую Codex-сессию со следующего сообщения",
                        "/cancel - очистить очередь и отложенные вложения, не трогая текущий запрос",
                        "/help - показать эту справку",
                    ]
                ),
            )
            return

        if command == "/status":
            await self._send_text(chat_id, self._format_status(state))
            return

        if command == "/sessions":
            try:
                await self._show_sessions_list(chat_id)
            except Exception as exc:
                self.logger.exception("Failed to show sessions for chat_id=%s", chat_id)
                await self._send_text(chat_id, f"Не удалось загрузить список сессий: {exc}")
            return

        if command == "/history":
            if not state.thread_id:
                await self._send_text(chat_id, "Текущая сессия не выбрана. Используйте /sessions.")
                return
            try:
                await self._show_session_history(chat_id, state.thread_id, offset=0)
            except Exception as exc:
                self.logger.exception("Failed to show history for chat_id=%s thread_id=%s", chat_id, state.thread_id)
                await self._send_text(chat_id, f"Не удалось загрузить историю сессии: {exc}")
            return

        if command == "/cancel":
            removed_requests: list[PendingRequest]
            removed_pending: list[StoredAttachment]
            async with self.state_lock:
                removed_requests = list(state.queue)
                removed_pending = list(state.pending_attachments)
                dropped = len(removed_requests)
                state.queue.clear()
                state.pending_attachments.clear()
                self._persist_locked()
            self._delete_request_attachments(removed_requests)
            self._delete_attachments(removed_pending)
            await self._send_text(chat_id, f"Очередь очищена. Удалено запросов: {dropped}.")
            return

        if command == "/new":
            removed_requests = []
            removed_pending = []
            async with self.state_lock:
                removed_requests = list(state.queue)
                removed_pending = list(state.pending_attachments)
                dropped = len(removed_requests)
                state.queue.clear()
                state.pending_attachments.clear()
                state.last_error = None
                if state.active_request is not None:
                    state.reset_session_after_current = True
                    notice = (
                        "Текущий запрос продолжает выполняться. Очередь очищена, "
                        "следующее сообщение пойдет уже в новую Codex-сессию."
                    )
                else:
                    state.thread_id = None
                    state.thread_name = None
                    state.reset_session_after_current = False
                    notice = "Сессия сброшена. Следующее сообщение откроет новую Codex-сессию."
                self._persist_locked()
            self._delete_request_attachments(removed_requests)
            self._delete_attachments(removed_pending)
            if dropped:
                notice = f"{notice}\nОчищено запросов из очереди: {dropped}."
            await self._send_text(chat_id, notice)
            return

        await self._send_text(chat_id, "Неизвестная команда. Используйте /help.")

    def _format_status(self, state: ChatState) -> str:
        lines = ["Статус Codex bridge"]
        if state.thread_id:
            lines.append(f"Сессия: {state.thread_id}")
            if state.thread_name:
                lines.append(f"Название: {state.thread_name}")
        else:
            lines.append("Сессия: будет создана на следующем сообщении")

        if state.active_request is not None:
            duration_seconds = 0
            if state.active_started_at:
                duration_seconds = max(int(time.time() - state.active_started_at), 0)
            lines.append(f"Выполняется: да, {duration_seconds} сек")
            lines.append(f"Текущий запрос: {state.active_request.text[:160]}")
            if state.active_request.attachments:
                lines.append(f"Вложений в текущем запросе: {len(state.active_request.attachments)}")
        else:
            lines.append("Выполняется: нет")

        lines.append(f"В очереди: {len(state.queue)}")
        if state.pending_attachments:
            lines.append(f"Отложенных вложений: {len(state.pending_attachments)}")
        if state.reset_session_after_current:
            lines.append("После текущего запроса сессия будет сброшена.")
        if state.last_error:
            lines.append(f"Последняя ошибка: {state.last_error}")
        if state.queue:
            lines.append("Следующий в очереди:")
            lines.append(state.queue[0].text[:160])
        return "\n".join(lines)

    async def _show_sessions_list(self, chat_id: int, edit_message: dict[str, Any] | None = None) -> None:
        sessions = self.session_index_store.list_recent(limit=7)
        async with self.state_lock:
            state = self.chats.get(chat_id)
            current_session_id = state.thread_id if state else None

        if not sessions:
            text = "Список Codex-сессий пока пуст."
            if edit_message is None:
                await self._send_text(chat_id, text)
            else:
                await self._edit_message_text(chat_id, int(edit_message["message_id"]), text)
            return

        lines = ["Последние 7 Codex-сессий на сервере:"]
        for index, session in enumerate(sessions, start=1):
            current_mark = " [текущая]" if session.session_id == current_session_id else ""
            lines.append(f"{index}. {session.thread_name}{current_mark}")
            lines.append(f"   {session.updated_at}")
            lines.append(f"   {session.session_id}")

        keyboard_rows: list[list[dict[str, str]]] = []
        for index, session in enumerate(sessions, start=1):
            keyboard_rows.append(
                [
                    {
                        "text": f"История {index}",
                        "callback_data": f"sessions:history:{session.session_id}",
                    },
                    {
                        "text": f"Подключить {index}",
                        "callback_data": f"sessions:use:{session.session_id}",
                    },
                ]
            )
        keyboard_rows.append([{"text": "Обновить список", "callback_data": "sessions:list"}])
        reply_markup = {"inline_keyboard": keyboard_rows}

        text = "\n".join(lines)
        if edit_message is None:
            await self._send_text(chat_id, text, reply_markup=reply_markup)
        else:
            await self._edit_message_text(
                chat_id,
                int(edit_message["message_id"]),
                text,
                reply_markup=reply_markup,
            )

    async def _attach_session(self, chat_id: int, session_id: str) -> None:
        summary = self.session_index_store.get(session_id)
        if summary is None:
            raise RuntimeError("Не нашел такую Codex-сессию в индексе.")

        async with self.state_lock:
            state = self.chats.get(chat_id)
            if state is None:
                state = ChatState(chat_id=chat_id)
                self.chats[chat_id] = state
            if state.active_request is not None or state.queue or state.pending_attachments:
                raise RuntimeError(
                    "Сначала дождитесь завершения текущего запроса или очистите очередь /cancel."
                )
            state.thread_id = summary.session_id
            state.thread_name = summary.thread_name
            state.last_error = None
            self._persist_locked()

        await self._send_text(
            chat_id,
            (
                "Текущий Telegram-чат переключен на выбранную Codex-сессию."
                f"\nСессия: {summary.session_id}"
                f"\nНазвание: {summary.thread_name}"
            ),
        )

    async def _show_session_history(self, chat_id: int, session_id: str, offset: int) -> None:
        summary, final_text, page_text, reply_markup = self._build_session_history_view(session_id, offset)
        if final_text:
            await self._send_text(
                chat_id,
                (
                    f"Последний финальный ответ в сессии `{summary.thread_name}`"
                    f"\n{summary.session_id}\n\n{final_text}"
                ),
            )
        await self._send_text(chat_id, page_text, reply_markup=reply_markup)

    async def _show_session_history_page(
        self,
        chat_id: int,
        session_id: str,
        offset: int,
        message: dict[str, Any],
    ) -> None:
        _, _, page_text, reply_markup = self._build_session_history_view(session_id, offset)
        await self._edit_message_text(
            chat_id,
            int(message["message_id"]),
            page_text,
            reply_markup=reply_markup,
        )

    def _build_session_history_view(
        self,
        session_id: str,
        offset: int,
        page_size: int = 6,
    ) -> tuple[CodexSessionSummary, str, str, dict[str, Any]]:
        summary = self.session_index_store.get(session_id)
        if summary is None:
            raise RuntimeError("Не нашел такую Codex-сессию в индексе.")

        messages = self.session_history_store.load_visible_messages(session_id)
        final_message: SessionVisibleMessage | None = None
        final_index: int | None = None
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].is_final_answer:
                final_message = messages[index]
                final_index = index
                break

        if final_index is None:
            timeline = messages
        else:
            timeline = [message for idx, message in enumerate(messages) if idx != final_index]

        ordered = list(reversed(timeline))
        safe_offset = max(offset, 0)
        chunk = ordered[safe_offset : safe_offset + page_size]

        lines = [
            f"История сессии: {summary.thread_name}",
            summary.session_id,
            f"Обновлено: {summary.updated_at}",
            "",
        ]

        if chunk:
            for index, item in enumerate(chunk, start=safe_offset + 1):
                role_label = "Вы" if item.role == "user" else "Codex"
                lines.append(f"{index}. {role_label}: {shorten_preview(item.text)}")
        else:
            lines.append("Больше видимых сообщений нет.")

        if final_message is not None:
            lines.extend(
                [
                    "",
                    "Последний final answer отправлен отдельным сообщением выше целиком.",
                ]
            )

        rows: list[list[dict[str, str]]] = [
            [
                {"text": "Подключить", "callback_data": f"history:use:{summary.session_id}"},
                {"text": "К списку", "callback_data": "sessions:list"},
            ]
        ]

        older_exists = safe_offset + page_size < len(ordered)
        newer_exists = safe_offset > 0
        nav_row: list[dict[str, str]] = []
        if newer_exists:
            nav_row.append(
                {
                    "text": "Новее",
                    "callback_data": f"history:page:{summary.session_id}:{max(safe_offset - page_size, 0)}",
                }
            )
        if older_exists:
            nav_row.append(
                {
                    "text": "Еще",
                    "callback_data": f"history:page:{summary.session_id}:{safe_offset + page_size}",
                }
            )
        if nav_row:
            rows.append(nav_row)

        return (
            summary,
            final_message.text if final_message is not None else "",
            "\n".join(lines),
            {"inline_keyboard": rows},
        )

    async def _get_or_create_state(self, chat_id: int, username: str | None) -> ChatState:
        async with self.state_lock:
            state = self.chats.get(chat_id)
            if state is None:
                state = ChatState(chat_id=chat_id)
                self.chats[chat_id] = state
            state.last_username = username
            state.last_seen_at = time.time()
            self._persist_locked()
            return state

    def _ensure_worker(self, state: ChatState) -> None:
        if state.worker_task and not state.worker_task.done():
            return
        state.worker_task = asyncio.create_task(self._worker_loop(state.chat_id))

    async def _worker_loop(self, chat_id: int) -> None:
        try:
            while True:
                async with self.state_lock:
                    state = self.chats[chat_id]
                    if state.active_request is None:
                        if not state.queue:
                            state.worker_task = None
                            self._persist_locked()
                            return
                        state.active_request = state.queue.pop(0)
                        state.active_started_at = time.time()
                        state.last_codex_event_at = state.active_started_at
                        state.last_heartbeat_at = None
                        if not state.thread_name:
                            state.thread_name = build_thread_name(state.active_request.text)
                        self._persist_locked()
                    request = state.active_request
                    thread_id = state.thread_id

                assert request is not None
                if thread_id:
                    await self._send_text(chat_id, "Продолжаю текущую Codex-сессию.")
                else:
                    await self._send_text(chat_id, "Открываю новую Codex-сессию.")

                success, error_text = await self._run_codex_request(chat_id, request)

                async with self.state_lock:
                    state = self.chats[chat_id]
                    if state.active_request and state.active_request.request_id == request.request_id:
                        state.active_request = None
                        state.active_started_at = None
                    if state.reset_session_after_current:
                        state.thread_id = None
                        state.thread_name = None
                        state.reset_session_after_current = False
                    state.last_error = error_text if not success else None
                    state.active_process = None
                    state.last_codex_event_at = None
                    state.last_heartbeat_at = None
                    self._persist_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Worker loop crashed for chat_id=%s", chat_id)
            async with self.state_lock:
                state = self.chats[chat_id]
                if state.active_process and state.active_process.returncode is None:
                    state.active_process.terminate()
                if state.active_request is not None:
                    state.queue.insert(0, state.active_request)
                state.active_request = None
                state.active_started_at = None
                state.active_process = None
                state.last_codex_event_at = None
                state.last_heartbeat_at = None
                state.last_error = "Worker loop crashed"
                state.worker_task = None
                self._persist_locked()
            await self._send_text(chat_id, "Bridge worker упал. Проверьте логи сервиса.")

    async def _run_codex_request(self, chat_id: int, request: PendingRequest) -> tuple[bool, str | None]:
        async with self.state_lock:
            state = self.chats[chat_id]
            thread_id = state.thread_id

        image_paths = [attachment.local_path for attachment in request.attachments if attachment.is_image]
        prompt = build_prompt_with_attachments(request.text, request.attachments)
        command = build_codex_command(
            self.config,
            thread_id=thread_id,
            prompt=prompt,
            image_paths=image_paths,
        )
        self.logger.info("Running Codex for chat_id=%s: %s", chat_id, command)

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.config.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async with self.state_lock:
            state = self.chats[chat_id]
            state.active_process = process
            self._upsert_session_index_locked(state)

        stderr_lines: list[str] = []
        stdout_task = asyncio.create_task(self._consume_codex_stdout(chat_id, process))
        stderr_task = asyncio.create_task(self._consume_stderr(process, stderr_lines))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(chat_id, request.request_id, process))

        returncode = await process.wait()
        heartbeat_task.cancel()
        consumer_results = await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        await asyncio.gather(heartbeat_task, return_exceptions=True)

        consumer_errors = [result for result in consumer_results if isinstance(result, Exception)]
        if consumer_errors:
            for error in consumer_errors:
                self.logger.error(
                    "Codex stream consumer failed for chat_id=%s",
                    chat_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
            error_text = "Мост не смог полностью обработать поток ответа Codex. Запрос можно повторить."
            await self._send_text(chat_id, error_text)
            return False, error_text

        if returncode == 0:
            return True, None

        error_text = summarize_codex_error(stderr_lines, returncode)
        await self._send_text(chat_id, error_text)
        return False, error_text

    async def _consume_codex_stdout(
        self,
        chat_id: int,
        process: asyncio.subprocess.Process,
    ) -> None:
        assert process.stdout is not None
        async for line in self._iter_stream_lines(process.stdout):
            if not line:
                continue
            if not line.startswith("{"):
                self.logger.debug("Non-JSON stdout from Codex: %s", line[:400])
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self.logger.warning("Failed to decode Codex JSONL line: %s", line[:400])
                continue
            await self._handle_codex_event(chat_id, payload)

    async def _consume_stderr(
        self,
        process: asyncio.subprocess.Process,
        stderr_lines: list[str],
    ) -> None:
        assert process.stderr is not None
        async for line in self._iter_stream_lines(process.stderr):
            if not line:
                continue
            stderr_lines.append(line)
            if len(stderr_lines) > 100:
                del stderr_lines[: len(stderr_lines) - 100]
            self.logger.warning("Codex stderr: %s", line[:800])

    async def _iter_stream_lines(self, stream: asyncio.StreamReader) -> AsyncIterator[str]:
        buffer = b""
        while True:
            chunk = await stream.read(STREAM_READ_CHUNK_BYTES)
            if not chunk:
                if buffer:
                    yield buffer.decode("utf-8", errors="replace").strip()
                return
            buffer += chunk
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                raw_line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                yield raw_line.decode("utf-8", errors="replace").strip()

    async def _handle_codex_event(self, chat_id: int, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        async with self.state_lock:
            state = self.chats.get(chat_id)
            if state is not None:
                state.last_codex_event_at = time.time()
                self._persist_locked()

        if event_type == "thread.started":
            thread_id = payload.get("thread_id")
            if thread_id:
                async with self.state_lock:
                    state = self.chats[chat_id]
                    state.thread_id = str(thread_id)
                    self._upsert_session_index_locked(state)
                    self._persist_locked()
            return

        if event_type == "item.started":
            item = payload.get("item") or {}
            if item.get("type") == "command_execution":
                self.logger.info(
                    "Codex command started for chat_id=%s: %s",
                    chat_id,
                    item.get("command"),
                )
            return

        if event_type != "item.completed":
            return

        item = payload.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                await self._send_text(chat_id, text)
            return

        if item_type == "command_execution":
            exit_code = item.get("exit_code")
            command = item.get("command")
            if exit_code not in {None, 0}:
                output = (item.get("aggregated_output") or "").strip()
                message = f"Команда Codex завершилась с ошибкой {exit_code}:\n{command}"
                if output:
                    message = f"{message}\n\n{output[:1500]}"
                await self._send_text(chat_id, message)

    async def _download_message_attachments(
        self,
        chat_id: int,
        message: dict[str, Any],
    ) -> list[StoredAttachment]:
        attachments: list[StoredAttachment] = []

        photos = message.get("photo") or []
        if photos:
            largest_photo = max(photos, key=lambda item: int(item.get("file_size", 0)))
            attachments.append(
                await self._download_telegram_attachment(
                    chat_id=chat_id,
                    file_id=str(largest_photo["file_id"]),
                    original_name=None,
                    mime_type="image/jpeg",
                    size_bytes=largest_photo.get("file_size"),
                    force_image=True,
                )
            )

        document = message.get("document")
        if document:
            mime_type = document.get("mime_type")
            original_name = document.get("file_name")
            attachments.append(
                await self._download_telegram_attachment(
                    chat_id=chat_id,
                    file_id=str(document["file_id"]),
                    original_name=original_name,
                    mime_type=mime_type,
                    size_bytes=document.get("file_size"),
                    force_image=is_supported_image(mime_type, original_name, original_name),
                )
            )

        return attachments

    async def _download_telegram_attachment(
        self,
        chat_id: int,
        file_id: str,
        original_name: str | None,
        mime_type: str | None,
        size_bytes: int | None,
        force_image: bool,
    ) -> StoredAttachment:
        file_info = await self._telegram_api("getFile", {"file_id": file_id})
        telegram_file_path = file_info.get("file_path")
        if not telegram_file_path:
            raise RuntimeError("Telegram не вернул путь к файлу")

        telegram_size = file_info.get("file_size") or size_bytes
        if telegram_size and int(telegram_size) > self.config.max_attachment_bytes:
            raise RuntimeError(
                f"Файл слишком большой: {int(telegram_size)} байт. "
                f"Лимит моста: {self.config.max_attachment_bytes} байт."
            )

        if self.client is None:
            raise RuntimeError("HTTP client is not initialized")

        file_url = (
            f"{self.config.telegram_api_base}/file/bot{self.config.telegram_bot_token}/"
            f"{quote(str(telegram_file_path), safe='/')}"
        )
        response = await self.client.get(file_url)
        response.raise_for_status()
        content = response.content
        if len(content) > self.config.max_attachment_bytes:
            raise RuntimeError(
                f"Файл слишком большой после скачивания: {len(content)} байт. "
                f"Лимит моста: {self.config.max_attachment_bytes} байт."
            )

        target_path = self._build_attachment_path(
            chat_id=chat_id,
            original_name=original_name,
            mime_type=mime_type,
            telegram_file_path=str(telegram_file_path),
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)

        kind = "image" if force_image else "file"
        detected_mime = mime_type or mimetypes.guess_type(target_path.name)[0]
        return StoredAttachment(
            kind=kind,
            local_path=str(target_path),
            original_name=original_name or Path(telegram_file_path).name,
            mime_type=detected_mime,
            size_bytes=len(content),
        )

    def _build_attachment_path(
        self,
        chat_id: int,
        original_name: str | None,
        mime_type: str | None,
        telegram_file_path: str,
    ) -> Path:
        now = datetime.now()
        source_name = original_name or Path(telegram_file_path).name or "attachment"
        suffix = Path(source_name).suffix
        if not suffix and mime_type:
            suffix = mimetypes.guess_extension(mime_type, strict=False) or ""
        safe_name = sanitize_filename(Path(source_name).stem or "attachment")
        filename = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{safe_name}{suffix}"
        return (
            self.config.uploads_dir
            / str(chat_id)
            / now.strftime("%Y")
            / now.strftime("%m")
            / now.strftime("%d")
            / filename
        )

    def _delete_request_attachments(self, requests: list[PendingRequest]) -> None:
        attachments: list[StoredAttachment] = []
        for request in requests:
            attachments.extend(request.attachments)
        self._delete_attachments(attachments)

    def _delete_attachments(self, attachments: list[StoredAttachment]) -> None:
        seen_paths: set[str] = set()
        for attachment in attachments:
            if attachment.local_path in seen_paths:
                continue
            seen_paths.add(attachment.local_path)
            try:
                Path(attachment.local_path).unlink(missing_ok=True)
            except OSError:
                self.logger.warning("Failed to delete attachment: %s", attachment.local_path, exc_info=True)

    async def _telegram_api(self, method: str, payload: dict[str, Any]) -> Any:
        if self.client is None:
            raise RuntimeError("HTTP client is not initialized")
        url = f"{self.config.telegram_api_base}/bot{self.config.telegram_bot_token}/{method}"
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        decoded = response.json()
        if not decoded.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {decoded}")
        return decoded.get("result")

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_telegram_text(text, self.config.max_message_chars)
        if not chunks:
            return
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            await self._telegram_api("sendMessage", payload)

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        trimmed = text.strip()
        if len(trimmed) > 4096:
            trimmed = shorten_preview(trimmed, 3900)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": trimmed,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._telegram_api("editMessageText", payload)
        except RuntimeError as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def _answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
        }
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        await self._telegram_api("answerCallbackQuery", payload)

    async def _heartbeat_loop(
        self,
        chat_id: int,
        request_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        sleep_interval = min(max(self.config.heartbeat_interval_seconds // 3, 5), 10)
        while True:
            await asyncio.sleep(sleep_interval)
            if process.returncode is not None:
                return

            async with self.state_lock:
                state = self.chats.get(chat_id)
                if state is None or state.active_request is None or state.active_request.request_id != request_id:
                    return
                now = time.time()
                last_event_at = state.last_codex_event_at or state.active_started_at or now
                last_heartbeat_at = state.last_heartbeat_at or 0.0
                if now - last_event_at < self.config.heartbeat_interval_seconds:
                    continue
                if now - last_heartbeat_at < self.config.heartbeat_interval_seconds:
                    continue
                state.last_heartbeat_at = now
                elapsed_seconds = max(int(now - (state.active_started_at or now)), 0)
                self._persist_locked()

            await self._send_text(
                chat_id,
                f"Codex всё ещё работает над запросом. Прошло примерно {elapsed_seconds} сек.",
            )

    def _upsert_session_index_locked(self, state: ChatState) -> None:
        if not state.thread_id:
            return
        thread_name = state.thread_name or f"Telegram chat {state.chat_id}"
        self.session_index_store.upsert(state.thread_id, thread_name)

    def _persist_locked(self) -> None:
        self.state_store.save(self.chats)


async def _async_main() -> None:
    config = BridgeConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bridge = TelegramCodexBridge(config)
    await bridge.run()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
