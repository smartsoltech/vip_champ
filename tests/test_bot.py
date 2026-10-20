"""Offline behavior tests: real repository, fake Telegram transport."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telebot import apihelper

from bot import MAX_IMPORT_BYTES, BotApplication
from db import Repository
from kb import generate_contact_keyboard, normalize_telegram_url

OWNER = 1001
OPERATOR = 1002
CLIENT = 2001
PASSWORD = "long audit password with spaces"


def message(text=None, user=OWNER, *, chat=None, chat_type="private", document=None):
    return SimpleNamespace(
        text=text, document=document, message_id=9000,
        chat=SimpleNamespace(id=user if chat is None else chat, type=chat_type),
        from_user=SimpleNamespace(
            id=user, is_bot=False, username=f"user{user}", first_name="Test", last_name="User"
        ),
    )


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBot:
    def __init__(self):
        self.sent = []
        self.documents = []
        self.deleted = []
        self.answers = []
        self.failure = {}
        self.download = b""
        self.counter = 0
        self.chats = {
            "@audit_channel": SimpleNamespace(id=-10077777, type="channel"),
            -10012345: SimpleNamespace(id=-10012345, type="supergroup", permissions=SimpleNamespace(can_send_messages=True)),
        }
        self.members = {}
        self.permission_checks = []

    def message_handler(self, **kwargs):
        return lambda callback: callback

    def callback_query_handler(self, **kwargs):
        return lambda callback: callback

    def send_message(self, chat_id, text, **kwargs):
        pending = self.failure.get(chat_id)
        if pending:
            raise pending.pop(0)
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise AssertionError("Telegram text length/type contract violated")
        self.counter += 1
        sent = SimpleNamespace(
            message_id=self.counter,
            chat=SimpleNamespace(id=chat_id, type="private" if isinstance(chat_id, int) and chat_id > 0 else "supergroup"),
            from_user=SimpleNamespace(id=999, is_bot=True),
            text=text, markup=kwargs.get("reply_markup"),
        )
        self.sent.append(sent)
        return sent

    def answer_callback_query(self, callback_id, **kwargs):
        self.answers.append((callback_id, kwargs))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def send_document(self, chat_id, document, **kwargs):
        self.documents.append((chat_id, document.read()))

    def get_file(self, file_id):
        return SimpleNamespace(file_path="synthetic.csv")

    def download_file(self, file_path):
        return self.download

    def get_me(self):
        return SimpleNamespace(id=999, username="audit_bot")

    def get_chat(self, chat_id):
        return self.chats[chat_id]

    def get_chat_member(self, chat_id, user_id):
        self.permission_checks.append((chat_id, user_id))
        member = self.members.get(chat_id, SimpleNamespace(status="administrator", can_post_messages=True))
        if isinstance(member, Exception):
            raise member
        return member


def telegram_error(code, retry_after=None):
    payload = {"ok": False, "error_code": code, "description": "synthetic API failure"}
    if retry_after is not None:
        payload["parameters"] = {"retry_after": retry_after}
    return apihelper.ApiTelegramException("sendMessage", None, payload)


class BotBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.repo = Repository(f"sqlite:///{(path / 'test.db').as_posix()}", backup_dir=path / "backups")
        self.repo.initialize()
        self.repo.add_admin("owner", PASSWORD, telegram_id=OWNER, is_superadmin=True)
        self.repo.add_admin("operator", PASSWORD, telegram_id=OPERATOR)
        self.bot = FakeBot()
        self.clock = Clock()
        self.settings = SimpleNamespace(
            state_ttl_seconds=300, auth_ttl_seconds=3600,
            auth_window_seconds=300, auth_max_attempts=5, auth_global_max_attempts=50,
            broadcast_interval_seconds=0.05, post_chat_ids=("@audit_channel", -10012345),
        )
        self.app = BotApplication(self.bot, self.repo, self.settings, clock=self.clock)

    def tearDown(self):
        self.app.stop()
        if hasattr(self.repo, "close"):
            self.repo.close()
        elif hasattr(self.repo, "engine"):
            self.repo.engine.dispose()
        self.temp.cleanup()

    def login(self, user=OWNER, username="owner"):
        self.app.handle_message(message("/settings", user))
        self.app.handle_message(message(f"{username} {PASSWORD}", user))

    def menu(self, user=OWNER):
        return next(item for item in reversed(self.bot.sent) if item.chat.id == user and item.markup is not None)

    def callback(self, action, *, user=OWNER, menu=None):
        menu = menu or self.menu(user)
        buttons = [button for row in menu.markup.to_dict()["inline_keyboard"] for button in row]
        nonce = buttons[0]["callback_data"].split(":")[1]
        call = SimpleNamespace(
            id=f"call-{len(self.bot.answers)}", data=f"admin:{nonce}:{action}",
            message=menu, from_user=message(user=user).from_user,
        )
        self.app.handle_callback(call)

    def client(self, chat_id=CLIENT):
        self.repo.get_or_create_client(chat_id, "Demo", "Client", False)

    def confirm(self, *, user=OWNER, decision="send", menu=None):
        menu = menu or self.menu(user)
        buttons = [button for row in menu.markup.to_dict()["inline_keyboard"] for button in row]
        data = next(button["callback_data"] for button in buttons if button["callback_data"].endswith(":" + decision))
        call = SimpleNamespace(
            id=f"confirmation-{len(self.bot.answers)}", data=data,
            message=menu, from_user=message(user=user).from_user,
        )
        self.app.handle_callback(call)
        return call

    def test_admin_aliases_require_login_and_open_owner_menu_after_valid_credentials(self):
        for command in ("/admin", "/admin@audit_bot"):
            with self.subTest(command=command):
                self.app.handle_message(message(command))
                self.assertNotIn((OWNER, OWNER), self.app._sessions)
                self.assertEqual(self.app._states[(OWNER, OWNER)].action, "login")
                self.assertIsNone(self.bot.sent[-1].markup)
                self.assertIn("логин и пароль", self.bot.sent[-1].text)
                self.app.handle_message(message(f"owner {PASSWORD}"))
                self.assertIn((OWNER, OWNER), self.app._sessions)
                self.assertIsNotNone(self.bot.sent[-1].markup)
                buttons = [
                    button
                    for row in self.bot.sent[-1].markup.to_dict()["inline_keyboard"]
                    for button in row
                ]
                self.assertTrue(any(button["callback_data"].endswith(":add_superadmin") for button in buttons))

    def test_admin_alias_does_not_grant_privileges_before_password_login(self):
        self.client()
        self.app.handle_message(message("/admin"))
        self.app.handle_message(message("/export_clients"))
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("unauthorized publication"))
        self.assertNotIn((OWNER, OWNER), self.app._sessions)
        self.assertEqual(self.bot.documents, [])
        self.assertTrue(all(item.chat.id == OWNER for item in self.bot.sent))
        self.assertTrue(all(item.markup is None for item in self.bot.sent))

    def test_admin_alias_rejects_owner_credentials_from_another_telegram_id(self):
        self.app.handle_message(message("/admin@audit_bot", CLIENT))
        self.app.handle_message(message(f"owner {PASSWORD}", CLIENT))
        self.app.handle_message(message("/export_clients", CLIENT))
        self.assertNotIn((CLIENT, CLIENT), self.app._sessions)
        self.assertIsNone(self.repo.get_admin_by_telegram_id(CLIENT))
        self.assertEqual(self.bot.documents, [])
        self.assertTrue(all(item.markup is None for item in self.bot.sent))

    def test_admin_aliases_enforce_private_chat_guard(self):
        with patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth:
            for command in ("/admin", "/admin@audit_bot"):
                with self.subTest(command=command):
                    before = len(self.bot.sent)
                    self.app.handle_message(message(command, chat=-50, chat_type="group"))
                    self.assertEqual(len(self.bot.sent), before + 1)
                    self.assertIn("только в личном чате", self.bot.sent[-1].text)
                    self.app.handle_message(message(f"owner {PASSWORD}", chat=-50, chat_type="group"))
            auth.assert_not_called()
        self.assertEqual(self.app._states, {})
        self.assertEqual(self.app._sessions, {})
        self.assertEqual(self.bot.documents, [])

    def test_public_post_is_denied_and_no_fixed_destination_remains(self):
        self.app.handle_message(message("/post_channel", CLIENT))
        self.app.handle_message(message("unauthorized post", CLIENT))
        self.assertTrue(all(item.chat.id == CLIENT for item in self.bot.sent))
        self.login()
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("authorized post"))
        self.assertFalse(any(item.chat.id < 0 for item in self.bot.sent))
        self.confirm()
        destinations = [item.chat.id for item in self.bot.sent if item.text == "authorized post" and item.chat.id != OWNER]
        self.assertEqual(destinations, [-10077777, -10012345])

    def test_group_authentication_and_commands_cannot_create_or_hijack_state(self):
        with patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth:
            for text in ["/settings", f"owner {PASSWORD}", "/add_admin", "4001 rogue another long password"]:
                self.app.handle_message(message(text, chat=-50, chat_type="group"))
            auth.assert_not_called()
        self.assertIsNone(self.repo.get_admin_by_telegram_id(4001))
        self.assertEqual(self.repo.list_clients(), [])
        self.app.handle_message(message("/start", chat=-50, chat_type="group"))
        self.assertEqual(self.repo.list_clients(), [])

    def test_bound_credentials_cannot_be_reused_by_a_different_telegram_user(self):
        self.app.handle_message(message("/settings", CLIENT))
        self.app.handle_message(message(f"owner {PASSWORD}", CLIENT))
        self.app.handle_message(message("/export_clients", CLIENT))
        self.assertEqual(self.bot.documents, [])
        self.assertNotIn((CLIENT, CLIENT), self.app._sessions)

    def test_callback_sender_and_nonce_are_checked_and_acknowledged_once(self):
        self.login()
        menu = self.menu()
        self.callback("change_manager_contact", user=CLIENT, menu=menu)
        self.app.handle_message(message("evil_contact", CLIENT))
        self.assertIsNone(self.repo.get_setting("manager_contact"))
        self.assertEqual(len(self.bot.answers), 1)
        self.app.handle_callback(SimpleNamespace(
            id="forged", data="admin:wrong:export_clients", message=menu,
            from_user=message().from_user,
        ))
        self.assertEqual(self.bot.documents, [])
        self.assertEqual(len(self.bot.answers), 2)

    def test_revoked_account_cannot_use_old_menu_or_finish_pending_action(self):
        self.login(OPERATOR, "operator")
        menu = self.menu(OPERATOR)
        self.callback("change_manager_contact", user=OPERATOR, menu=menu)
        self.repo.remove_admin("operator", actor_id=OWNER)
        self.app.handle_message(message("attacker_name", OPERATOR))
        self.callback("export_clients", user=OPERATOR, menu=menu)
        self.assertIsNone(self.repo.get_setting("manager_contact"))
        self.assertEqual(self.bot.documents, [])

    def test_password_reset_invalidates_session_and_pending_privileged_input(self):
        self.login(OPERATOR, "operator")
        self.callback("change_manager_contact", user=OPERATOR)
        self.repo.update_admin("operator", password="new password for revoked sessions")
        self.app.handle_message(message("cannot_save_after_reset", OPERATOR))
        self.assertIsNone(self.repo.get_setting("manager_contact"))
        self.assertNotIn((OPERATOR, OPERATOR), self.app._sessions)
        self.app.handle_message(message("/export_clients", OPERATOR))
        self.assertEqual(self.bot.documents, [])

    def test_recreated_account_cannot_inherit_session_for_same_telegram_id(self):
        self.login(OPERATOR, "operator")
        self.repo.remove_admin("operator", actor_id=OWNER)
        self.repo.add_admin("replacement", "replacement long password", telegram_id=OPERATOR, is_superadmin=True)
        self.app.handle_message(message("/add_admin", OPERATOR))
        self.app.handle_message(message("4001 inherited another long password", OPERATOR))
        self.assertIsNone(self.repo.get_admin_by_telegram_id(4001))
        self.assertNotIn((OPERATOR, OPERATOR), self.app._sessions)

    def test_role_change_requires_fresh_authentication(self):
        self.login(OPERATOR, "operator")
        self.repo.update_admin("operator", is_superadmin=True)
        self.app.handle_message(message("/get_admins", OPERATOR))
        self.assertNotIn((OPERATOR, OPERATOR), self.app._sessions)
        self.login(OPERATOR, "operator")
        self.app.handle_message(message("/get_admins", OPERATOR))
        self.assertTrue(any("owner · ID" in item.text for item in self.bot.sent))

    def test_revocation_during_password_hashing_prevents_new_superadmin(self):
        import db

        self.repo.add_admin("backup", PASSWORD, telegram_id=5001, is_superadmin=True)
        self.login()
        self.app.handle_message(message("/add_superadmin"))
        real_hash = db.generate_password_hash

        def revoke_during_hash(password):
            self.repo.remove_admin("owner", actor_id=5001)
            return real_hash(password)

        with patch.object(db, "generate_password_hash", side_effect=revoke_during_hash):
            self.app.handle_message(message("4001 blocked another long password"))
        self.assertIsNone(self.repo.get_admin_by_telegram_id(4001))

    def test_revocation_during_import_backup_prevents_database_write(self):
        self.login(OPERATOR, "operator")
        self.app.handle_message(message("/import_clients", OPERATOR))
        content = b"First Name,Last Name,Chat ID\nImported,Client,30001\n"
        self.bot.download = content
        real_backup = self.repo.backup_database

        def revoke_during_backup():
            backup = real_backup()
            self.repo.remove_admin("operator", actor_id=OWNER)
            return backup

        with patch.object(self.repo, "backup_database", side_effect=revoke_during_backup):
            self.app.handle_message(message(user=OPERATOR, document=SimpleNamespace(
                file_name="clients.csv", file_size=len(content), file_id="synthetic"
            )))
        self.assertEqual(self.repo.list_clients(), [])

    def test_ordinary_admin_cannot_manage_other_admins_even_with_forged_action(self):
        self.login(OPERATOR, "operator")
        menu = self.menu(OPERATOR)
        self.callback("add_superadmin", user=OPERATOR, menu=menu)
        self.app.handle_message(message("4001 rogue another long password", OPERATOR))
        self.app.handle_message(message("/remove_admin", OPERATOR))
        self.app.handle_message(message("owner", OPERATOR))
        self.assertIsNone(self.repo.get_admin_by_telegram_id(4001))
        self.assertIsNotNone(self.repo.get_admin_by_telegram_id(OWNER))

    def test_inline_add_admin_and_password_with_spaces_work_once(self):
        self.login()
        self.callback("add_admin")
        with patch.object(self.repo, "add_admin", wraps=self.repo.add_admin) as add:
            self.app.handle_message(message("4001 newadmin a long password with spaces"))
            self.assertEqual(add.call_count, 1)
        self.assertIsNotNone(self.repo.authenticate_admin("newadmin", "a long password with spaces", 4001))
        self.assertFalse(self.repo.get_admin_by_telegram_id(4001).is_superadmin)
        self.app.handle_message(message("/settings", 4001))
        self.app.handle_message(message("newadmin a long password with spaces", 4001))
        self.assertIn((4001, 4001), self.app._sessions)

    def test_add_superadmin_requires_strong_password_and_rechecks_role(self):
        self.login()
        self.callback("add_superadmin")
        self.app.handle_message(message("4001 newadmin short"))
        self.assertIsNone(self.repo.get_admin_by_telegram_id(4001))
        self.app.handle_message(message("4001 newadmin another long password"))
        self.assertTrue(self.repo.get_admin_by_telegram_id(4001).is_superadmin)

    def test_buttons_replace_pending_action_and_start_cancel_clear_it(self):
        self.login()
        menu = self.menu()
        self.callback("change_manager_contact", menu=menu)
        self.callback("change_channel_link", menu=menu)
        self.app.handle_message(message("https://t.me/example_channel"))
        self.assertIsNone(self.repo.get_setting("manager_contact"))
        self.assertEqual(self.repo.get_setting("channel_link"), "https://t.me/example_channel")
        for cancellation in ["/cancel", "/start"]:
            self.app.handle_message(message("/change_manager_contact"))
            self.app.handle_message(message(cancellation))
            self.app.handle_message(message("should_not_save"))
            self.assertIsNone(self.repo.get_setting("manager_contact"))

    def test_nontext_does_not_crash_login_or_privileged_steps(self):
        self.app.handle_message(message("/settings"))
        self.app.handle_message(message(None))
        self.app.handle_message(message(f"owner {PASSWORD}"))
        for action in ["add_admin", "add_superadmin", "change_manager_contact", "send_all", "import_clients"]:
            self.app.handle_message(message("/" + action))
            self.app.handle_message(message(None))
        self.assertEqual(len(self.repo.list_admins()), 2)
        self.assertEqual(self.repo.pending_broadcasts(), [])
        self.app.handle_message(message("/get_settings"))
        self.assertTrue(any("Настройки пока не заданы" in item.text for item in self.bot.sent))

    def test_state_and_session_expiration_prevent_late_changes(self):
        self.login()
        self.app.handle_message(message("/change_manager_contact"))
        self.clock.advance(self.settings.state_ttl_seconds + 1)
        self.app.handle_message(message("late_contact"))
        self.assertIsNone(self.repo.get_setting("manager_contact"))
        self.clock.advance(self.settings.auth_ttl_seconds)
        self.app.handle_message(message("/export_clients"))
        self.assertEqual(self.bot.documents, [])

    def test_auth_rate_limit_is_per_user_and_global(self):
        self.settings.auth_max_attempts = 2
        self.settings.auth_global_max_attempts = 3
        with patch.object(self.repo, "authenticate_admin", return_value=None) as auth:
            for user, count in [(OWNER, 4), (OPERATOR, 3)]:
                self.app.handle_message(message("/settings", user))
                for _ in range(count):
                    self.app.handle_message(message("bad wrong password", user))
            self.assertEqual(auth.call_count, 3)
            self.clock.advance(301)
            self.app.handle_message(message("/settings", OWNER))
            self.app.handle_message(message("bad wrong password", OWNER))
            self.assertEqual(auth.call_count, 4)

    def test_separate_password_input_preserves_leading_and_trailing_whitespace(self):
        for password in ("  leading synthetic password", "trailing synthetic password  ", "newline synthetic password\r\n"):
            with self.subTest(kind=password.split()[0]):
                self.repo.update_admin("owner", password=password)
                self.clock.advance(301)
                self.app.handle_message(message("/admin"))
                self.app.handle_message(message("owner"))
                self.assertEqual(self.app._states[(OWNER, OWNER)].action, "login_password")
                with patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth:
                    self.app.handle_message(message(password))
                auth.assert_called_once_with("owner", password, OWNER)
                self.assertIn((OWNER, OWNER), self.app._sessions)

    def test_copied_line_endings_fall_back_only_after_exact_password_fails(self):
        for suffix in ("\n", "\r\n"):
            with self.subTest(suffix=repr(suffix)):
                self.clock.advance(301)
                self.app.handle_message(message("/admin"))
                with patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth:
                    self.app.handle_message(message(f"owner {PASSWORD}{suffix}"))
                self.assertEqual(auth.call_args_list[0].args, ("owner", PASSWORD + suffix, OWNER))
                self.assertEqual(auth.call_args_list[1].args, ("owner", PASSWORD, OWNER))
                self.assertIn((OWNER, OWNER), self.app._sessions)
        self.app.handle_message(message("/admin"))
        self.app.handle_message(message(f"owner {PASSWORD} "))
        self.assertNotIn((OWNER, OWNER), self.app._sessions)

    def test_unknown_ids_cannot_exhaust_bound_admin_quota_and_are_still_limited(self):
        with (
            patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth,
            patch.object(self.repo, "get_admin_by_telegram_id", wraps=self.repo.get_admin_by_telegram_id) as lookup,
        ):
            for user in range(3000, 3010):
                self.app.handle_message(message("/admin", user))
                for _ in range(6):
                    self.app.handle_message(message("owner synthetic wrong password", user))
            self.assertEqual(lookup.call_count, 50)
            auth.assert_not_called()
            self.assertEqual(len(self.app._global_attempts), 0)
            self.login()
            auth.assert_called_once_with("owner", PASSWORD, OWNER)
        self.assertIn((OWNER, OWNER), self.app._sessions)

    def test_separate_password_is_private_expires_and_keeps_generic_denial(self):
        self.app.handle_message(message("/admin", CLIENT))
        self.app.handle_message(message("owner", CLIENT))
        self.app.handle_message(message(PASSWORD, CLIENT))
        outsider_denial = self.bot.sent[-1].text
        self.app.handle_message(message("/admin"))
        self.app.handle_message(message("owner"))
        self.app.handle_message(message("synthetic incorrect password"))
        self.assertEqual(self.bot.sent[-1].text, outsider_denial)
        self.clock.advance(self.settings.state_ttl_seconds + 1)
        with patch.object(self.repo, "authenticate_admin", wraps=self.repo.authenticate_admin) as auth:
            self.app.handle_message(message(PASSWORD))
            auth.assert_not_called()
        self.assertNotIn((OWNER, OWNER), self.app._sessions)

    def test_credentials_and_internal_exception_details_are_never_sent_or_logged(self):
        self.app.handle_message(message("/settings"))
        secret = "synthetic-password-for-log-check"
        with (
            patch.object(self.repo, "authenticate_admin", side_effect=RuntimeError(secret)),
            self.assertLogs("bot", level="WARNING") as captured,
        ):
            self.app.handle_message(message(f"owner {secret}"))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn(secret, "\n".join(item.text for item in self.bot.sent))
        self.assertTrue(self.bot.deleted)

    def test_contact_urls_are_normalized_and_missing_or_invalid_links_are_hidden(self):
        for value in ["example_name", "@example_name", "https://t.me/example_name", "https://telegram.me/example_name"]:
            self.assertEqual(normalize_telegram_url(value), "https://t.me/example_name")
        for invalid in ["javascript:alert(1)", "https://t.me.evil.test/example", "https://t.me/@example", "", None]:
            with self.assertRaises((TypeError, ValueError)):
                normalize_telegram_url(invalid)
        self.assertIsNone(generate_contact_keyboard({}))
        keyboard = generate_contact_keyboard({"channel_link": "https://t.me/example_name", "manager_contact": "invalid value"})
        self.assertEqual(len(keyboard.to_dict()["inline_keyboard"]), 1)

    def test_export_is_authenticated_and_can_be_imported_back(self):
        self.client()
        self.app.handle_message(message("/export_clients"))
        self.assertEqual(self.bot.documents, [])
        self.login()
        self.app.handle_message(message("/export_clients"))
        self.assertEqual(len(self.bot.documents), 1)
        result = self.repo.import_clients(self.bot.documents[0][1])
        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.duplicates, 1)

    def test_large_import_sends_bounded_summary_and_no_temporary_file(self):
        self.login()
        content = ("First Name,Last Name,Chat ID\n" + "".join(f"User{i},Test,{30000+i}\n" for i in range(200))).encode()
        self.bot.download = content
        self.app.handle_message(message("/import_clients"))
        self.app.handle_message(message(document=SimpleNamespace(
            file_name="CLIENTS.CSV", file_size=len(content), file_id="synthetic-file"
        )))
        self.assertEqual(len(self.repo.list_clients()), 200)
        self.assertTrue(any("Добавлено: 200" in item.text for item in self.bot.sent))
        self.assertTrue(all(len(item.text) <= 4096 for item in self.bot.sent))
        self.assertFalse((Path(self.temp.name) / "temp.csv").exists())

    def test_import_rejects_large_file_without_downloading(self):
        self.login()
        self.app.handle_message(message("/import_clients"))
        with patch.object(self.bot, "download_file", wraps=self.bot.download_file) as download:
            self.app.handle_message(message(document=SimpleNamespace(
                file_name="too-big.csv", file_size=MAX_IMPORT_BYTES + 1, file_id="synthetic"
            )))
            download.assert_not_called()

    def test_broadcast_preserves_4096_character_text_and_durable_progress(self):
        self.client()
        self.login()
        self.app.handle_message(message("/send_all"))
        self.app.handle_message(message("X" * 4096))
        self.assertEqual(self.repo.pending_broadcasts(), [])
        self.confirm()
        jobs = self.repo.pending_broadcasts()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(self.app.process_broadcast_once())
        sent = [item for item in self.bot.sent if item.chat.id == CLIENT]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].text, "X" * 4096)
        self.assertEqual(self.repo.get_broadcast_status(jobs[0]["id"])["sent"], 1)

    def test_broadcast_unknown_delivery_is_not_automatically_retried(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "test text")
        self.bot.failure[CLIENT] = [TimeoutError("network contains no safe delivery result")]
        self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["uncertain"], 1)
        self.clock.advance(100)
        self.assertFalse(self.app.process_broadcast_once())
        self.repo.recover_broadcasts()
        self.assertFalse(self.app.process_broadcast_once())
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_broadcast_retries_failed_persistence_without_sending_again(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "send once despite DB failure")
        real_record = self.repo.record_broadcast_result
        calls = 0

        def interrupted_record(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic DB interruption before commit")
            return real_record(*args, **kwargs)

        with patch.object(self.repo, "record_broadcast_result", side_effect=interrupted_record):
            with self.assertRaises(RuntimeError):
                self.app.process_broadcast_once()
            self.assertEqual(self.repo.get_broadcast_status(job_id)["sending"], 1)
            self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["sent"], 1)
        self.assertEqual(sum(item.chat.id == CLIENT for item in self.bot.sent), 1)
        self.assertIsNone(self.app._pending_delivery)

    def test_broadcast_reconciles_lost_commit_response_without_duplicate_send(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "send once despite lost DB acknowledgment")
        real_record = self.repo.record_broadcast_result
        calls = 0

        def committed_but_interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_record(*args, **kwargs)
            if calls == 1:
                raise RuntimeError("synthetic lost acknowledgment after successful commit")
            return result

        with patch.object(self.repo, "record_broadcast_result", side_effect=committed_but_interrupted):
            with self.assertRaises(RuntimeError):
                self.app.process_broadcast_once()
            self.assertEqual(self.repo.get_broadcast_status(job_id)["sent"], 1)
            self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(sum(item.chat.id == CLIENT for item in self.bot.sent), 1)
        self.assertIsNone(self.app._pending_delivery)

    def test_broadcast_429_defers_and_does_not_spin_or_lose_recipient(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "test text")
        self.bot.failure[CLIENT] = [telegram_error(429, 30)]
        self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["pending"], 1)
        self.assertFalse(self.app.process_broadcast_once())
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_broadcast_permanent_failure_and_revocation_are_safe(self):
        self.client()
        job_id = self.repo.create_broadcast(OPERATOR, "test text")
        self.repo.remove_admin("operator", actor_id=OWNER)
        self.assertFalse(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["status"], "canceled")
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_broadcast_preview_requires_owner_confirmation_and_is_consumed_once(self):
        self.client()
        self.login()
        self.app.handle_message(message("/send_all"))
        self.app.handle_message(message("confirmed broadcast"))
        preview = self.menu()
        self.assertEqual(self.repo.pending_broadcasts(), [])
        self.confirm(user=CLIENT, menu=preview)
        self.assertEqual(self.repo.pending_broadcasts(), [])
        call = self.confirm(menu=preview)
        self.app.handle_callback(call)
        self.assertEqual(len(self.repo.pending_broadcasts()), 1)
        self.assertEqual(len(self.bot.answers), 3)
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_parallel_confirmation_clicks_create_only_one_broadcast(self):
        self.client()
        self.login()
        self.app.handle_message(message("/send_all"))
        self.app.handle_message(message("send exactly once"))
        preview = self.menu()
        barrier = threading.Barrier(2)

        def click():
            barrier.wait(timeout=5)
            self.confirm(menu=preview)

        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.repo.pending_broadcasts()), 1)

    def test_expired_replaced_and_canceled_previews_cannot_enqueue(self):
        self.client()
        self.login()
        for invalidate in ("expire", "/cancel", "/start", "/get_settings"):
            with self.subTest(invalidate=invalidate):
                self.app.handle_message(message("/send_all"))
                self.app.handle_message(message("discarded preview"))
                preview = self.menu()
                if invalidate == "expire":
                    self.clock.advance(self.settings.state_ttl_seconds + 1)
                else:
                    self.app.handle_message(message(invalidate))
                self.confirm(menu=preview)
                self.assertEqual(self.repo.pending_broadcasts(), [])

    def test_password_reset_between_preview_and_confirmation_revokes_send(self):
        self.client()
        self.login(OPERATOR, "operator")
        self.app.handle_message(message("/send_all", OPERATOR))
        self.app.handle_message(message("must never be sent", OPERATOR))
        preview = self.menu(OPERATOR)
        self.repo.update_admin("operator", password="replacement synthetic password")
        self.confirm(user=OPERATOR, menu=preview)
        self.assertEqual(self.repo.pending_broadcasts(), [])
        self.assertNotIn((OPERATOR, OPERATOR), self.app._sessions)

    def test_cancel_distinguishes_input_from_queue_and_enforces_job_ownership(self):
        self.client()
        owner_job = self.repo.create_broadcast(OWNER, "owned by superadmin")
        operator_job = self.repo.create_broadcast(OPERATOR, "owned by operator")
        self.login(OPERATOR, "operator")
        self.app.handle_message(message("/cancel", OPERATOR))
        self.assertTrue(any("/broadcast_cancel" in item.text for item in self.bot.sent[-2:]))
        self.assertIsNotNone(self.bot.sent[-1].markup)
        self.assertEqual(self.repo.get_broadcast_status(operator_job)["status"], "pending")
        self.app.handle_message(message(f"/broadcast_cancel {owner_job}", OPERATOR))
        self.assertEqual(self.repo.get_broadcast_status(owner_job)["status"], "pending")
        self.app.handle_message(message(f"/broadcast_cancel {operator_job}", OPERATOR))
        self.assertEqual(self.repo.get_broadcast_status(operator_job)["status"], "canceled")
        self.login()
        self.app.handle_message(message(f"/broadcast_cancel {owner_job}"))
        self.assertEqual(self.repo.get_broadcast_status(owner_job)["status"], "canceled")
        self.assertFalse(self.app.process_broadcast_once())
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_stop_opts_out_queued_broadcast_and_start_resubscribes(self):
        self.app.handle_message(message("/start", CLIENT))
        job_id = self.repo.create_broadcast(OWNER, "before opt out")
        self.app.handle_message(message("/stop", CLIENT))
        self.assertFalse(self.repo.is_client_subscribed(CLIENT))
        self.assertEqual(self.repo.get_broadcast_status(job_id)["canceled"], 1)
        self.assertFalse(self.app.process_broadcast_once())
        self.app.handle_message(message("/help", CLIENT))
        self.assertFalse(self.repo.is_client_subscribed(CLIENT))
        self.app.handle_message(message("/start", CLIENT))
        self.assertTrue(self.repo.is_client_subscribed(CLIENT))
        next_job = self.repo.create_broadcast(OWNER, "after subscription")
        self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(next_job)["sent"], 1)
        self.assertFalse(any("before opt out" in item.text for item in self.bot.sent))

    def test_optout_after_claim_is_checked_again_before_delivery(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "do not send after optout")
        claim = self.repo.get_broadcast_batch

        def claim_then_opt_out(*args, **kwargs):
            rows = claim(*args, **kwargs)
            self.repo.set_client_subscription(CLIENT, False)
            return rows

        with patch.object(self.repo, "get_broadcast_batch", side_effect=claim_then_opt_out):
            self.assertTrue(self.app.process_broadcast_once())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["canceled"], 1)
        self.assertFalse(any(item.chat.id == CLIENT for item in self.bot.sent))

    def test_worker_progress_does_not_advance_on_failed_persistence_and_recovers(self):
        self.client()
        job_id = self.repo.create_broadcast(OWNER, "test worker health")
        before = self.app.last_worker_progress
        self.clock.advance(5)
        with patch.object(self.repo, "record_broadcast_result", side_effect=RuntimeError("synthetic unavailable write")):
            with self.assertRaises(RuntimeError):
                self.app.process_broadcast_once()
        self.assertFalse(self.app.worker_healthy)
        self.assertEqual(self.app.last_worker_progress, before)
        self.clock.advance(5)
        self.assertTrue(self.app.process_broadcast_once())
        self.assertTrue(self.app.worker_healthy)
        self.assertEqual(self.app.last_worker_progress, self.clock())
        self.assertEqual(self.repo.get_broadcast_status(job_id)["sent"], 1)
        self.assertEqual(sum(item.chat.id == CLIENT for item in self.bot.sent), 1)

    def test_simultaneous_users_cannot_overwrite_each_others_settings_conversation(self):
        self.login()
        self.login(OPERATOR, "operator")
        self.app.handle_message(message("/change_manager_contact"))
        self.app.handle_message(message("/change_channel_link", OPERATOR))
        barrier = threading.Barrier(2)
        def send(user, text):
            barrier.wait(timeout=5)
            self.app.handle_message(message(text, user))
        threads = [
            threading.Thread(target=send, args=(OWNER, "manager_contact")),
            threading.Thread(target=send, args=(OPERATOR, "channel_contact")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.repo.get_setting("manager_contact"), "https://t.me/manager_contact")
        self.assertEqual(self.repo.get_setting("channel_link"), "https://t.me/channel_contact")

    def test_slow_import_download_does_not_block_another_users_start(self):
        self.login()
        self.app.handle_message(message("/import_clients"))
        download_started = threading.Event()
        release_download = threading.Event()
        other_finished = threading.Event()
        content = b"First Name,Last Name,Chat ID\nImport,Client,30001\n"

        def slow_download(file_path):
            download_started.set()
            if not release_download.wait(10):
                raise TimeoutError("test download was not released")
            return content

        def other_user():
            self.app.handle_message(message("/start", CLIENT))
            other_finished.set()

        importer = threading.Thread(target=self.app.handle_message, args=(message(
            document=SimpleNamespace(file_name="clients.csv", file_size=len(content), file_id="synthetic")
        ),))
        visitor = threading.Thread(target=other_user)
        with patch.object(self.bot, "download_file", side_effect=slow_download):
            try:
                importer.start()
                self.assertTrue(download_started.wait(5))
                visitor.start()
                self.assertTrue(other_finished.wait(3), "One user's download blocked another user's /start")
                self.assertTrue(any(item.chat.id == CLIENT for item in self.bot.sent))
            finally:
                release_download.set()
                importer.join(timeout=5)
                if visitor.ident is not None:
                    visitor.join(timeout=5)
        self.assertFalse(importer.is_alive())
        self.assertFalse(visitor.is_alive())
        self.assertEqual(self.app._conversation_locks, {})

    def test_post_reports_partial_and_uncertain_outcomes_without_repeating(self):
        self.login()
        self.bot.failure[-10012345] = [TimeoutError("unknown outcome")]
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("post once"))
        self.confirm()
        self.assertEqual(sum(item.text == "post once" and item.chat.id != OWNER for item in self.bot.sent), 1)
        self.assertTrue(any("результат неизвестен" in item.text for item in self.bot.sent))

    def test_publication_targets_are_explicit_and_independent_of_contact_links(self):
        self.repo.set_setting("channel_link", "https://t.me/some_other_channel")
        self.login()
        self.app.handle_message(message("/get_settings"))
        settings_text = "\n".join(item.text for item in self.bot.sent)
        self.assertIn("POST_CHAT_IDS", settings_text)
        self.assertIn("@audit_channel", settings_text)
        self.assertIn("-10012345", settings_text)
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("explicit targets"))
        self.assertTrue(any("@audit_channel → -10077777" in item.text for item in self.bot.sent))
        call = self.confirm()
        self.app.handle_callback(call)
        destinations = [item.chat.id for item in self.bot.sent if item.chat.id < 0]
        self.assertEqual(destinations, [-10077777, -10012345])
        self.assertEqual(self.bot.permission_checks, [(-10077777, 999), (-10012345, 999)] * 2)

    def test_publication_preflight_fails_closed_for_missing_membership_or_permission(self):
        self.login()
        for member in (
            SimpleNamespace(status="left"),
            SimpleNamespace(status="administrator", can_post_messages=False),
            telegram_error(400),
        ):
            with self.subTest(member=type(member).__name__):
                self.bot.members[-10077777] = member
                self.app.handle_message(message("/post_channel"))
                self.app.handle_message(message("blocked publication"))
                self.assertEqual(self.app._confirmations, {})
                self.assertFalse(any(item.chat.id < 0 for item in self.bot.sent))
        self.assertTrue(any("Проверьте" in item.text for item in self.bot.sent))

    def test_publication_permission_loss_between_preview_and_confirm_prevents_all_sends(self):
        self.login()
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("permission changed"))
        preview = self.menu()
        self.bot.members[-10012345] = SimpleNamespace(status="left")
        call = self.confirm(menu=preview)
        self.assertFalse(any(item.chat.id < 0 for item in self.bot.sent))
        self.bot.members.clear()
        self.app.handle_callback(call)
        self.assertFalse(any(item.chat.id < 0 for item in self.bot.sent))

    def test_publication_changed_resolved_target_requires_new_preview(self):
        self.login()
        self.app.handle_message(message("/post_channel"))
        self.app.handle_message(message("target changed"))
        preview = self.menu()
        self.bot.chats["@audit_channel"] = SimpleNamespace(id=-10088888, type="channel")
        self.confirm(menu=preview)
        self.assertFalse(any(item.chat.id < 0 for item in self.bot.sent))
        self.assertTrue(any("Адресаты публикации изменились" in item.text for item in self.bot.sent))

    def test_confirmation_lost_commit_response_does_not_create_second_job(self):
        self.client()
        self.login()
        self.app.handle_message(message("/send_all"))
        self.app.handle_message(message("single confirmed job"))
        real_create = self.repo.create_broadcast

        def commit_without_response(*args, **kwargs):
            real_create(*args, **kwargs)
            raise RuntimeError("synthetic commit acknowledgement lost")

        with (
            patch.object(self.repo, "create_broadcast", side_effect=commit_without_response) as create,
            self.assertLogs("bot", level="WARNING"),
        ):
            call = self.confirm()
            self.app.handle_callback(call)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(len(self.repo.pending_broadcasts()), 1)

    def test_broadcast_cancel_rechecks_revision_inside_repository(self):
        self.client()
        job_id = self.repo.create_broadcast(OPERATOR, "keep job until authorized cancellation")
        self.login(OPERATOR, "operator")
        real_cancel = self.repo.request_cancel_broadcast

        def revoke_during_cancel(*args, **kwargs):
            self.repo.update_admin("operator", password="replacement cancellation password")
            return real_cancel(*args, **kwargs)

        with patch.object(self.repo, "request_cancel_broadcast", side_effect=revoke_during_cancel):
            self.app.handle_message(message(f"/broadcast_cancel {job_id}", OPERATOR))
        self.assertEqual(self.repo.get_broadcast_status(job_id)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
