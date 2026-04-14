from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.codex_telegram_bridge import (
    BridgeConfig,
    BridgeStateStore,
    ChatState,
    CodexSessionHistoryStore,
    CodexSessionIndexStore,
    PendingRequest,
    StoredAttachment,
    TelegramCodexBridge,
    build_prompt_with_attachments,
    build_thread_name,
    build_codex_command,
    is_supported_image,
    normalize_username,
    parse_allowed_chat_ids,
    parse_allowed_usernames,
    sanitize_filename,
    split_telegram_text,
    summarize_codex_error,
)


class CodexTelegramBridgeHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BridgeConfig(
            telegram_bot_token="token",
            allowed_usernames={"vvo888"},
            allowed_chat_ids={12345},
            private_only=True,
            poll_timeout_seconds=50,
            telegram_api_base="https://api.telegram.org",
            workdir="/opt/customer-project",
            cli_path="/usr/bin/codex",
            dangerous_bypass=True,
            skip_git_repo_check=True,
            profile="telegram_fast",
            model="gpt-5.4",
            state_file=Path("/tmp/codex-telegram-state.json"),
            session_index_file=Path("/tmp/codex-telegram-session-index.jsonl"),
            uploads_dir=Path("/tmp/codex-telegram-uploads"),
            max_attachment_bytes=50 * 1024 * 1024,
            max_message_chars=128,
            heartbeat_interval_seconds=30,
            log_level="INFO",
        )

    def test_parse_allowed_usernames_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(parse_allowed_usernames(" vvo888,@VVO888, other "), {"vvo888", "other"})

    def test_parse_allowed_chat_ids_skips_empty_chunks(self) -> None:
        self.assertEqual(parse_allowed_chat_ids("123,,456"), {123, 456})

    def test_normalize_username_strips_at_prefix(self) -> None:
        self.assertEqual(normalize_username("@VvO888 "), "vvo888")

    def test_build_codex_command_for_new_thread(self) -> None:
        command = build_codex_command(self.config, thread_id=None, prompt="hello")
        self.assertEqual(command[:4], ["/usr/bin/codex", "-p", "telegram_fast", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("-C", command)
        self.assertEqual(command[-2], "--")
        self.assertEqual(command[-1], "hello")

    def test_build_codex_command_for_resume(self) -> None:
        command = build_codex_command(self.config, thread_id="thread-123", prompt="continue")
        self.assertEqual(command[:5], ["/usr/bin/codex", "-p", "telegram_fast", "exec", "resume"])
        self.assertIn("--json", command)
        self.assertNotIn("-C", command)
        self.assertEqual(command[-3], "--")
        self.assertIn("thread-123", command)
        self.assertEqual(command[-1], "continue")

    def test_build_codex_command_adds_images(self) -> None:
        command = build_codex_command(
            self.config,
            thread_id="thread-123",
            prompt="continue",
            image_paths=["/tmp/a.png", "/tmp/b.jpg"],
        )
        self.assertEqual(command.count("--image"), 2)
        self.assertIn("/tmp/a.png", command)
        self.assertIn("/tmp/b.jpg", command)
        self.assertEqual(command[-3], "--")

    def test_split_telegram_text_respects_limit(self) -> None:
        payload = "alpha beta gamma delta epsilon zeta eta theta"
        chunks = split_telegram_text(payload, limit=12)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))

    def test_build_thread_name_normalizes_whitespace_and_truncates(self) -> None:
        name = build_thread_name("  alpha   beta\n gamma  " * 10, limit=20)
        self.assertLessEqual(len(name), 20)
        self.assertTrue(name.startswith("alpha beta gamma"))

    def test_sanitize_filename_strips_unsafe_chars(self) -> None:
        self.assertEqual(sanitize_filename("../bad file?.png"), "bad_file_.png")

    def test_is_supported_image_detects_allowed_types(self) -> None:
        self.assertTrue(is_supported_image("image/png", "screen.png", None))
        self.assertTrue(is_supported_image(None, "screen.jpg", None))
        self.assertFalse(is_supported_image("image/gif", "anim.gif", None))

    def test_state_store_requeues_active_request_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            store = BridgeStateStore(state_path)
            state = ChatState(
                chat_id=1,
                thread_id="thread-1",
                thread_name="Telegram test thread",
                pending_attachments=[
                    StoredAttachment(
                        kind="file",
                        local_path="/tmp/pending.txt",
                        original_name="pending.txt",
                    )
                ],
                queue=[PendingRequest(request_id="queued", text="queued text", created_at=1.0)],
                active_request=PendingRequest(
                    request_id="active",
                    text="active text",
                    created_at=2.0,
                    attachments=[
                        StoredAttachment(
                            kind="image",
                            local_path="/tmp/image.png",
                            original_name="image.png",
                        )
                    ],
                ),
                active_started_at=123.0,
                reset_session_after_current=True,
            )
            store.save({1: state})

            restored = store.load()
            self.assertIn(1, restored)
            restored_state = restored[1]
            self.assertIsNone(restored_state.active_request)
            self.assertIsNone(restored_state.active_started_at)
            self.assertEqual(restored_state.thread_name, "Telegram test thread")
            self.assertEqual(len(restored_state.pending_attachments), 1)
            self.assertEqual([item.request_id for item in restored_state.queue], ["active", "queued"])
            self.assertEqual(len(restored_state.queue[0].attachments), 1)

    def test_build_prompt_with_attachments_includes_paths(self) -> None:
        prompt = build_prompt_with_attachments(
            "Проверь файлы",
            [
                StoredAttachment(
                    kind="image",
                    local_path="/tmp/screen.png",
                    original_name="screen.png",
                    mime_type="image/png",
                ),
                StoredAttachment(
                    kind="file",
                    local_path="/tmp/report.csv",
                    original_name="report.csv",
                    mime_type="text/csv",
                ),
            ],
        )
        self.assertIn("Проверь файлы", prompt)
        self.assertIn("/tmp/screen.png", prompt)
        self.assertIn("/tmp/report.csv", prompt)

    def test_session_index_store_upserts_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            index_path = Path(tmp_dir) / "session_index.jsonl"
            store = CodexSessionIndexStore(index_path)
            store.upsert("thread-1", "First name")
            store.upsert("thread-2", "Second name")
            store.upsert("thread-1", "Updated first name")

            lines = [line for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertIn('"id":"thread-1"', lines[0])
            self.assertIn('"thread_name":"Updated first name"', lines[0])

    def test_session_index_store_lists_recent_and_gets_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            index_path = Path(tmp_dir) / "session_index.jsonl"
            index_path.write_text(
                "\n".join(
                    [
                        '{"id":"thread-2","thread_name":"Second","updated_at":"2026-04-05T10:00:00Z"}',
                        '{"id":"thread-1","thread_name":"First","updated_at":"2026-04-05T09:00:00Z"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            store = CodexSessionIndexStore(index_path)
            recent = store.list_recent(limit=7)
            self.assertEqual([item.session_id for item in recent], ["thread-2", "thread-1"])
            found = store.get("thread-1")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.thread_name, "First")

    def test_session_history_store_filters_commentary_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_root = Path(tmp_dir)
            session_file = sessions_root / "2026" / "04" / "05" / "rollout-2026-04-05T12-00-00-thread-1.jsonl"
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(
                "\n".join(
                    [
                        '{"timestamp":"2026-04-05T12:00:00Z","type":"session_meta","payload":{}}',
                        '{"timestamp":"2026-04-05T12:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"первый вопрос"}]}}',
                        '{"timestamp":"2026-04-05T12:00:02Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"скрытый шаг"}]}}',
                        '{"timestamp":"2026-04-05T12:00:03Z","type":"response_item","payload":{"type":"function_call","name":"exec_command"}}',
                        '{"timestamp":"2026-04-05T12:00:04Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"итоговый ответ"}]}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            store = CodexSessionHistoryStore(sessions_root)
            messages = store.load_visible_messages("thread-1")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0].role, "user")
            self.assertEqual(messages[1].phase, "final_answer")
            self.assertEqual(messages[1].text, "итоговый ответ")

    def test_iter_stream_lines_handles_oversized_codex_line(self) -> None:
        bridge = TelegramCodexBridge(self.config)

        async def _collect() -> list[str]:
            stream = asyncio.StreamReader()
            long_text = "x" * 200_000
            payload = (
                '{"type":"item.completed","item":{"type":"agent_message","text":"'
                + long_text
                + '"}}\n'
                + '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
            )
            stream.feed_data(payload.encode("utf-8"))
            stream.feed_eof()
            return [line async for line in bridge._iter_stream_lines(stream)]

        lines = asyncio.run(_collect())
        self.assertEqual(len(lines), 2)
        self.assertIn('"text":"done"', lines[1])
        self.assertGreater(len(lines[0]), 100_000)

    def test_media_group_is_combined_into_single_request(self) -> None:
        bridge = TelegramCodexBridge(self.config)
        state = ChatState(chat_id=12345)
        bridge.chats[state.chat_id] = state
        sent_messages: list[str] = []

        async def _send_text(chat_id: int, text: str, reply_markup=None) -> None:
            sent_messages.append(text)

        bridge._send_text = _send_text  # type: ignore[method-assign]
        bridge._ensure_worker = lambda current_state: None  # type: ignore[method-assign]

        async def _run() -> None:
            await bridge._buffer_media_group_message(
                state=state,
                media_group_id="group-1",
                text="caption text",
                attachments=[
                    StoredAttachment(kind="image", local_path="/tmp/a.jpg", original_name="a.jpg")
                ],
                source_message_id=10,
            )
            await bridge._buffer_media_group_message(
                state=state,
                media_group_id="group-1",
                text="",
                attachments=[
                    StoredAttachment(kind="image", local_path="/tmp/b.jpg", original_name="b.jpg"),
                    StoredAttachment(kind="image", local_path="/tmp/c.jpg", original_name="c.jpg"),
                ],
                source_message_id=11,
            )
            buffer = bridge.media_groups[(state.chat_id, "group-1")]
            assert buffer.flush_task is not None
            buffer.flush_task.cancel()
            await bridge._flush_media_group(state.chat_id, "group-1")

        asyncio.run(_run())
        self.assertEqual(len(state.queue), 1)
        self.assertEqual(state.queue[0].text, "caption text")
        self.assertEqual(len(state.queue[0].attachments), 3)
        self.assertTrue(any("Вложений в запросе: 3." in message for message in sent_messages))

    def test_summarize_codex_error_maps_cloudflare_403_to_short_message(self) -> None:
        message = summarize_codex_error(
            [
                "2026-04-04T13:50:55.900278Z ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: HTTP error: 403 Forbidden, url: wss://chatgpt.com/backend-api/codex/responses"
            ],
            1,
        )
        self.assertIn("403 Forbidden", message)
        self.assertIn("прокси", message)


if __name__ == "__main__":
    unittest.main()
