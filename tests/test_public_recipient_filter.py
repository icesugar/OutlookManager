import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PublicRecipientFilterTest(unittest.TestCase):
    def make_item(self, message_id: str, to_email: str | None) -> main.EmailItem:
        return main.EmailItem(
            message_id=message_id,
            folder="INBOX",
            subject="Subject",
            from_email="sender@example.com",
            date="2026-01-01T00:00:00",
            to_email=to_email,
        )

    def test_public_list_keeps_only_exact_recipient_ignore_case(self):
        response = main.EmailListResponse(
            email_id="User@Outlook.com",
            folder_view="all",
            page=1,
            page_size=10,
            total_emails=4,
            emails=[
                self.make_item("INBOX-1", "User Name <USER@outlook.com>"),
                self.make_item("INBOX-2", "Alias <alias@outlook.com>"),
                self.make_item("INBOX-3", "Other <other@example.com>, user@OUTLOOK.com"),
                self.make_item("INBOX-4", None),
            ],
        )

        filtered = main.filter_email_list_response_by_recipient(response, "user@outlook.com")

        self.assertEqual(["INBOX-1", "INBOX-3"], [email.message_id for email in filtered.emails])
        self.assertEqual(2, filtered.total_emails)

    def test_public_detail_requires_exact_recipient_ignore_case(self):
        detail = main.EmailDetailsResponse(
            message_id="INBOX-1",
            subject="Subject",
            from_email="sender@example.com",
            to_email="Alias <alias@outlook.com>",
            date="2026-01-01T00:00:00",
        )

        self.assertFalse(main.email_matches_recipient(detail, "user@outlook.com"))

    def test_admin_list_can_reuse_exact_recipient_filter(self):
        original_require_authenticated = main.require_authenticated
        original_get_account_credentials = main.get_account_credentials
        original_list_emails = main.list_emails

        async def fake_get_account_credentials(email_id):
            return main.AccountCredentials(
                email=email_id,
                refresh_token="refresh",
                client_id="client",
            )

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=2,
                emails=[
                    self.make_item("INBOX-1", "User Name <USER@outlook.com>"),
                    self.make_item("INBOX-2", "Alias <alias@outlook.com>"),
                ],
            )

        try:
            main.require_authenticated = lambda *args, **kwargs: {"auth_type": "session"}
            main.get_account_credentials = fake_get_account_credentials
            main.list_emails = fake_list_emails

            filtered = asyncio.run(
                main.get_emails(
                    SimpleNamespace(),
                    "user@outlook.com",
                    folder="all",
                    page=1,
                    page_size=10,
                    refresh=False,
                    filter_aliases=True,
                )
            )
            unfiltered = asyncio.run(
                main.get_emails(
                    SimpleNamespace(),
                    "user@outlook.com",
                    folder="all",
                    page=1,
                    page_size=10,
                    refresh=False,
                    filter_aliases=False,
                )
            )
        finally:
            main.require_authenticated = original_require_authenticated
            main.get_account_credentials = original_get_account_credentials
            main.list_emails = original_list_emails

        self.assertEqual(["INBOX-1"], [email.message_id for email in filtered.emails])
        self.assertEqual(1, filtered.total_emails)
        self.assertEqual(["INBOX-1", "INBOX-2"], [email.message_id for email in unfiltered.emails])
        self.assertEqual(2, unfiltered.total_emails)


class AllEmailListTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_sqlite_file = main.ALL_EMAIL_SQLITE_FILE
        main.ALL_EMAIL_SQLITE_FILE = Path(self.temp_dir.name) / "all_emails.sqlite3"
        main.initialize_all_email_sqlite()

    def tearDown(self):
        main.clear_all_email_query_cache()
        if hasattr(main, "reset_all_email_sqlite_refresh_status_for_tests"):
            main.reset_all_email_sqlite_refresh_status_for_tests()
        main.ALL_EMAIL_SQLITE_FILE = self.original_sqlite_file
        self.temp_dir.cleanup()

    def seed_all_email_items(self, items: list[main.AllEmailItem]):
        by_account: dict[str, list[main.EmailItem]] = {}
        for item in items:
            by_account.setdefault(item.account_email, []).append(item)
        for account_email, account_items in by_account.items():
            main.upsert_all_email_sqlite_items(account_email, account_items)

    def make_all_item(self, account_email: str, message_id: str, date: str) -> main.AllEmailItem:
        return main.AllEmailItem(
            account_email=account_email,
            message_id=message_id,
            folder="INBOX",
            subject="Subject",
            from_email="sender@example.com",
            date=date,
            to_email=account_email,
        )

    def test_all_email_list_sorts_across_accounts(self):
        original_load_accounts_data = main.load_accounts_data

        try:
            main.load_accounts_data = lambda: {
                "older@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "newer@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            self.seed_all_email_items([
                self.make_all_item("older@example.com", "INBOX-1", "2026-01-01T08:00:00"),
                self.make_all_item("newer@example.com", "INBOX-2", "2026-01-02T08:00:00"),
            ])

            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data

        self.assertEqual(["INBOX-2", "INBOX-1"], [email.message_id for email in response.emails])
        self.assertEqual(["newer@example.com", "older@example.com"], [email.account_email for email in response.emails])
        self.assertEqual(2, response.total_emails)

    def test_all_email_filtered_total_uses_sqlite_matches(self):
        original_load_accounts_data = main.load_accounts_data

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "second@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            first = self.make_all_item("first@example.com", "INBOX-1", "2026-01-01T08:00:00")
            first.subject = "Invoice received"
            second = self.make_all_item("second@example.com", "INBOX-2", "2026-01-02T08:00:00")
            second.from_email = "billing@example.com"
            unrelated = self.make_all_item("second@example.com", "INBOX-3", "2026-01-03T08:00:00")
            unrelated.subject = "Welcome"
            self.seed_all_email_items([first, second, unrelated])

            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10, search="invoice"))
        finally:
            main.load_accounts_data = original_load_accounts_data

        self.assertEqual(1, response.total_emails)
        self.assertEqual(["INBOX-1"], [email.message_id for email in response.emails])

    def test_all_email_filters_category_tag_read_state_and_aliases(self):
        original_load_accounts_data = main.load_accounts_data
        original_load_account_classifications_data = main.load_account_classifications_data
        original_load_email_tags_data = main.load_email_tags_data

        matching = self.make_all_item("vip@example.com", "INBOX-1", "2026-01-04T08:00:00")
        matching.is_read = True
        matching.to_email = "VIP User <vip@example.com>"
        alias = self.make_all_item("vip@example.com", "INBOX-2", "2026-01-05T08:00:00")
        alias.is_read = True
        alias.to_email = "Alias <alias@example.com>"
        unread = self.make_all_item("vip@example.com", "INBOX-3", "2026-01-06T08:00:00")
        unread.to_email = "VIP User <vip@example.com>"
        standard = self.make_all_item("standard@example.com", "INBOX-4", "2026-01-07T08:00:00")
        standard.is_read = True

        try:
            main.load_accounts_data = lambda: {
                "vip@example.com": {
                    "refresh_token": "refresh",
                    "client_id": "client",
                    "category_key": "vip",
                    "tag_keys": ["registered"],
                },
                "standard@example.com": {
                    "refresh_token": "refresh",
                    "client_id": "client",
                    "category_key": "standard",
                    "tag_keys": ["trial"],
                },
            }
            main.load_account_classifications_data = lambda: {
                "categories": {
                    "vip": {"name_zh": "重要客户", "name_en": "VIP"},
                    "standard": {"name_zh": "普通客户", "name_en": "Standard"},
                },
                "tags": {
                    "registered": {"name_zh": "已注册", "name_en": "Registered"},
                    "trial": {"name_zh": "试用", "name_en": "Trial"},
                },
            }
            main.load_email_tags_data = lambda: {"emails": {}}
            self.seed_all_email_items([matching, alias, unread, standard])

            filtered = main.query_all_email_sqlite_response(
                page=1,
                page_size=10,
                category_filter="vip",
                tag_filter="registered",
                read_status="read",
                filter_aliases=True,
            )
            with_aliases = main.query_all_email_sqlite_response(
                page=1,
                page_size=10,
                category_filter="vip",
                tag_filter="registered",
                read_status="read",
                filter_aliases=False,
            )
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.load_account_classifications_data = original_load_account_classifications_data
            main.load_email_tags_data = original_load_email_tags_data

        self.assertEqual(["INBOX-1"], [email.message_id for email in filtered.emails])
        self.assertEqual(1, filtered.total_emails)
        self.assertEqual(["INBOX-2", "INBOX-1"], [email.message_id for email in with_aliases.emails])
        self.assertEqual(2, with_aliases.total_emails)

    def test_collected_all_email_includes_account_category_and_tags(self):
        original_list_emails = main.list_emails
        original_load_account_classifications_data = main.load_account_classifications_data

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=1,
                emails=[
                    main.EmailItem(
                        message_id="INBOX-1",
                        folder="INBOX",
                        subject="Subject",
                        from_email="sender@example.com",
                        date="2026-01-01T08:00:00",
                        to_email=str(credentials.email),
                    )
                ],
            )

        try:
            main.list_emails = fake_list_emails
            main.load_account_classifications_data = lambda: {
                "categories": {
                    "vip": {"name_zh": "重要客户", "name_en": "VIP"}
                },
                "tags": {
                    "registered": {"name_zh": "已注册", "name_en": "Registered"}
                },
            }

            items, total = asyncio.run(main.collect_account_all_emails(
                main.AccountCredentials(
                    email="user@example.com",
                    refresh_token="refresh",
                    client_id="client",
                    category_key="vip",
                    tag_keys=["registered"],
                ),
                fetch_limit=10,
                search_term="",
                date_from=None,
                date_to=None,
                refresh=False,
            ))
        finally:
            main.list_emails = original_list_emails
            main.load_account_classifications_data = original_load_account_classifications_data

        self.assertEqual(1, total)
        self.assertEqual("vip", items[0].account_category_key)
        self.assertEqual("重要客户", items[0].account_category.name_zh)
        self.assertEqual(["registered"], items[0].account_tag_keys)
        self.assertEqual("已注册", items[0].account_tag_details[0].name_zh)

    def test_all_email_initial_load_reads_sqlite_without_remote_collect(self):
        original_load_accounts_data = main.load_accounts_data
        original_collect_account_all_emails = main.collect_account_all_emails

        async def fake_collect(credentials, fetch_limit, search_term, date_from, date_to, refresh):
            raise AssertionError("SQLite list should not collect remote emails")

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "second@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.collect_account_all_emails = fake_collect

            started = time.perf_counter()
            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
            elapsed = time.perf_counter() - started
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.collect_account_all_emails = original_collect_account_all_emails

        self.assertLess(elapsed, 0.12)
        self.assertFalse(response.refreshing)
        self.assertEqual([], response.emails)
        self.assertEqual(0, response.pending_accounts)

    def test_all_email_repeated_query_stays_on_sqlite(self):
        original_load_accounts_data = main.load_accounts_data
        original_collect_account_all_emails = main.collect_account_all_emails

        async def fake_collect(credentials, fetch_limit, search_term, date_from, date_to, refresh):
            raise AssertionError("SQLite list should not collect remote emails")

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.collect_account_all_emails = fake_collect
            self.seed_all_email_items([
                self.make_all_item("first@example.com", "INBOX-1", "2026-01-01T08:00:00"),
            ])

            first = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
            second = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.collect_account_all_emails = original_collect_account_all_emails

        self.assertFalse(first.refreshing)
        self.assertFalse(second.refreshing)
        self.assertEqual(["INBOX-1"], [email.message_id for email in second.emails])

    def test_all_email_response_reports_sqlite_sync_age(self):
        original_load_accounts_data = main.load_accounts_data

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            self.seed_all_email_items([
                self.make_all_item("first@example.com", "INBOX-1", "2026-01-01T08:00:00"),
            ])

            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data

        self.assertFalse(response.refreshing)
        self.assertIsNotNone(response.cache_updated_at)
        self.assertIsInstance(response.cache_age_seconds, int)

    def test_refresh_all_email_sqlite_uses_latest_timestamp(self):
        original_load_accounts_data = main.load_accounts_data
        original_list_emails = main.list_emails

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=3,
                emails=[
                    self.make_all_item(str(credentials.email), "INBOX-2", "2026-01-02T08:00:00"),
                    self.make_all_item(str(credentials.email), "INBOX-1", "2026-01-01T08:00:00"),
                    self.make_all_item(str(credentials.email), "INBOX-0", "2025-12-31T08:00:00"),
                ],
            )

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            self.seed_all_email_items([
                self.make_all_item("first@example.com", "INBOX-1", "2026-01-01T08:00:00"),
            ])
            main.list_emails = fake_list_emails

            refresh_result = asyncio.run(main.refresh_all_account_emails_to_sqlite())
            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.list_emails = original_list_emails

        self.assertEqual(1, refresh_result.total_accounts)
        self.assertEqual(1, refresh_result.refreshed_accounts)
        self.assertEqual([], refresh_result.failed_accounts)
        self.assertEqual(["INBOX-2", "INBOX-1"], [email.message_id for email in response.emails])

    def test_refresh_empty_account_requests_and_stores_latest_five(self):
        original_load_accounts_data = main.load_accounts_data
        original_list_emails = main.list_emails
        seen_page_sizes = []

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            seen_page_sizes.append(page_size)
            items = [
                self.make_all_item(str(credentials.email), f"INBOX-{index}", f"2026-01-{index:02d}T08:00:00")
                for index in range(7, 0, -1)
            ]
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=len(items),
                emails=items[:page_size],
            )

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.list_emails = fake_list_emails

            refresh_result = asyncio.run(main.refresh_all_account_emails_to_sqlite())
            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.list_emails = original_list_emails

        self.assertEqual([5], seen_page_sizes)
        self.assertEqual(5, refresh_result.inserted_or_updated)
        self.assertEqual(
            ["INBOX-7", "INBOX-6", "INBOX-5", "INBOX-4", "INBOX-3"],
            [email.message_id for email in response.emails],
        )

    def test_refresh_account_sqlite_skips_items_outside_retention_window(self):
        original_list_emails = main.list_emails
        recent_date = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat()
        old_date = (datetime.utcnow() - timedelta(days=60)).replace(microsecond=0).isoformat()

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=2,
                emails=[
                    self.make_all_item(str(credentials.email), "INBOX-recent", recent_date),
                    self.make_all_item(str(credentials.email), "INBOX-old", old_date),
                ],
            )

        try:
            main.list_emails = fake_list_emails

            inserted = asyncio.run(main.refresh_account_emails_to_sqlite(
                main.AccountCredentials(
                    email="first@example.com",
                    refresh_token="refresh",
                    client_id="client",
                ),
                retention_days=30,
            ))
        finally:
            main.list_emails = original_list_emails

        connection = main.get_all_email_sqlite_connection()
        try:
            rows = connection.execute(
                "SELECT message_id FROM all_email_index ORDER BY received_at_ts DESC"
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(1, inserted)
        self.assertEqual(["INBOX-recent"], [row["message_id"] for row in rows])

    def test_refresh_existing_account_checks_latest_five_and_keeps_old_sqlite_rows(self):
        original_load_accounts_data = main.load_accounts_data
        original_list_emails = main.list_emails
        seen_page_sizes = []

        async def fake_list_emails(credentials, folder, page, page_size, force_refresh=False):
            seen_page_sizes.append(page_size)
            items = [
                self.make_all_item(str(credentials.email), f"INBOX-{index}", f"2026-01-{index:02d}T08:00:00")
                for index in range(7, 0, -1)
            ]
            return main.EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=len(items),
                emails=items[:page_size],
            )

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            self.seed_all_email_items([
                self.make_all_item("first@example.com", "INBOX-3", "2026-01-03T08:00:00"),
                self.make_all_item("first@example.com", "INBOX-0", "2025-12-31T08:00:00"),
            ])
            main.list_emails = fake_list_emails

            refresh_result = asyncio.run(main.refresh_all_account_emails_to_sqlite())
            response = asyncio.run(main.list_all_account_emails(page=1, page_size=10))
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.list_emails = original_list_emails

        self.assertEqual([5], seen_page_sizes)
        self.assertEqual(5, refresh_result.inserted_or_updated)
        self.assertEqual(
            ["INBOX-7", "INBOX-6", "INBOX-5", "INBOX-4", "INBOX-3", "INBOX-0"],
            [email.message_id for email in response.emails],
        )

    def test_refresh_all_email_sqlite_honors_request_concurrency(self):
        original_load_accounts_data = main.load_accounts_data
        original_refresh_account_emails_to_sqlite = main.refresh_account_emails_to_sqlite
        active_count = 0
        max_active_count = 0

        async def fake_refresh_account(credentials, retention_days=None):
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            await asyncio.sleep(0.01)
            active_count -= 1
            return 1

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "second@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.refresh_account_emails_to_sqlite = fake_refresh_account

            result = asyncio.run(main.refresh_all_account_emails_to_sqlite(concurrency=1))
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.refresh_account_emails_to_sqlite = original_refresh_account_emails_to_sqlite

        self.assertEqual(1, max_active_count)
        self.assertEqual(2, result.refreshed_accounts)
        self.assertEqual(2, result.inserted_or_updated)

    def test_start_all_email_sqlite_refresh_runs_in_background_and_reports_status(self):
        original_load_accounts_data = main.load_accounts_data
        original_refresh_account_emails_to_sqlite = main.refresh_account_emails_to_sqlite

        async def fake_refresh_account(credentials, retention_days=None):
            await asyncio.sleep(0.01)
            return 1

        async def run_refresh():
            status = await main.start_all_email_sqlite_refresh(concurrency=1)
            self.assertTrue(status.running)
            self.assertEqual(2, status.total_accounts)
            self.assertEqual(1, status.concurrency)
            self.assertEqual(0, status.checked_accounts)

            while True:
                current = main.get_all_email_sqlite_refresh_status()
                if not current.running:
                    return current
                await asyncio.sleep(0.01)

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "second@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.refresh_account_emails_to_sqlite = fake_refresh_account

            final_status = asyncio.run(run_refresh())
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.refresh_account_emails_to_sqlite = original_refresh_account_emails_to_sqlite

        self.assertFalse(final_status.running)
        self.assertEqual(2, final_status.checked_accounts)
        self.assertEqual(2, final_status.refreshed_accounts)
        self.assertEqual(2, final_status.inserted_or_updated)
        self.assertEqual([], final_status.failed_accounts)
        self.assertIsNotNone(final_status.completed_at)

    def test_start_all_email_sqlite_refresh_is_single_flight(self):
        original_load_accounts_data = main.load_accounts_data
        original_refresh_account_emails_to_sqlite = main.refresh_account_emails_to_sqlite
        started_accounts = []

        async def fake_refresh_account(credentials, retention_days=None):
            started_accounts.append(str(credentials.email))
            await asyncio.sleep(0.03)
            return 1

        async def run_refresh():
            first = await main.start_all_email_sqlite_refresh(concurrency=1)
            second = await main.start_all_email_sqlite_refresh(concurrency=5)
            self.assertTrue(first.running)
            self.assertTrue(second.running)
            self.assertEqual(first.task_id, second.task_id)
            self.assertEqual(1, second.concurrency)

            while main.get_all_email_sqlite_refresh_status().running:
                await asyncio.sleep(0.01)

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
                "second@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            main.refresh_account_emails_to_sqlite = fake_refresh_account

            asyncio.run(run_refresh())
        finally:
            main.load_accounts_data = original_load_accounts_data
            main.refresh_account_emails_to_sqlite = original_refresh_account_emails_to_sqlite

        self.assertEqual(["first@example.com", "second@example.com"], started_accounts)

    def test_background_refresh_prunes_index_and_body_cache_outside_retention_window(self):
        original_refresh_account_emails_to_sqlite = main.refresh_account_emails_to_sqlite
        original_load_accounts_data = main.load_accounts_data
        recent_date = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat()
        old_date = (datetime.utcnow() - timedelta(days=60)).replace(microsecond=0).isoformat()
        observed_retention_days = []

        async def fake_refresh_account(credentials, retention_days=None):
            observed_retention_days.append(retention_days)
            return 0

        try:
            main.load_accounts_data = lambda: {
                "first@example.com": {"refresh_token": "refresh", "client_id": "client"},
            }
            self.seed_all_email_items([
                self.make_all_item("first@example.com", "INBOX-recent", recent_date),
                self.make_all_item("first@example.com", "INBOX-old", old_date),
            ])
            main.upsert_all_email_body_cache("first@example.com", main.EmailDetailsResponse(
                message_id="INBOX-recent",
                subject="Recent",
                from_email="sender@example.com",
                to_email="first@example.com",
                date=recent_date,
                body_plain="recent body",
            ))
            main.upsert_all_email_body_cache("first@example.com", main.EmailDetailsResponse(
                message_id="INBOX-old",
                subject="Old",
                from_email="sender@example.com",
                to_email="first@example.com",
                date=old_date,
                body_plain="old body",
            ))
            main.refresh_account_emails_to_sqlite = fake_refresh_account
            main.reset_all_email_sqlite_refresh_status_for_tests()
            main.update_all_email_sqlite_refresh_status(
                task_id="task",
                running=True,
                total_accounts=1,
                checked_accounts=0,
                refreshed_accounts=0,
                inserted_or_updated=0,
                failed_accounts=[],
                account_errors={},
                started_at=main.utc_now_iso(),
                completed_at=None,
                concurrency=1,
                trigger="manual",
                error="",
            )

            asyncio.run(main.run_all_email_sqlite_refresh_task(
                "task",
                [("first@example.com", {"refresh_token": "refresh", "client_id": "client"})],
                1,
                retention_days=30,
            ))
            response = main.query_all_email_sqlite_response(page=1, page_size=10)
        finally:
            main.refresh_account_emails_to_sqlite = original_refresh_account_emails_to_sqlite
            main.load_accounts_data = original_load_accounts_data

        self.assertEqual([30], observed_retention_days)
        self.assertEqual(["INBOX-recent"], [email.message_id for email in response.emails])
        self.assertIsNotNone(main.get_all_email_body_cache("first@example.com", "INBOX-recent"))
        self.assertIsNone(main.get_all_email_body_cache("first@example.com", "INBOX-old"))


class EmailReadStateTest(unittest.TestCase):
    def test_graph_email_mark_read_patches_server_and_clears_account_cache(self):
        original_get_access_token = main.get_access_token
        original_graph_api_patch = main.graph_api_patch
        original_clear_email_cache = main.clear_email_cache
        patch_calls = []
        cleared_accounts = []

        async def fake_get_access_token(credentials):
            return "access-token"

        async def fake_graph_api_patch(access_token, path, payload):
            patch_calls.append((access_token, path, payload))
            return {}

        try:
            main.get_access_token = fake_get_access_token
            main.graph_api_patch = fake_graph_api_patch
            main.clear_email_cache = lambda email=None: cleared_accounts.append(email)

            credentials = main.AccountCredentials(
                email="user@example.com",
                refresh_token="refresh",
                client_id="client",
                auth_method="graph",
            )
            asyncio.run(main.mark_email_as_read(
                credentials,
                main.build_graph_message_id("inbox", "A/B=="),
            ))
        finally:
            main.get_access_token = original_get_access_token
            main.graph_api_patch = original_graph_api_patch
            main.clear_email_cache = original_clear_email_cache

        self.assertEqual([
            ("access-token", "/me/messages/A%2FB%3D%3D", {"isRead": True})
        ], patch_calls)
        self.assertEqual(["user@example.com"], cleared_accounts)

    def test_email_details_uses_body_cache_and_applies_current_tags(self):
        original_sqlite_file = main.ALL_EMAIL_SQLITE_FILE
        original_get_graph_email_details = main.get_graph_email_details
        original_load_account_classifications_data = main.load_account_classifications_data
        original_load_email_tags_data = main.load_email_tags_data
        temp_dir = tempfile.TemporaryDirectory()

        async def fail_graph_fetch(credentials, message_id):
            raise AssertionError("Cached details should not fetch Graph API")

        try:
            main.ALL_EMAIL_SQLITE_FILE = Path(temp_dir.name) / "all_emails.sqlite3"
            main.initialize_all_email_sqlite()
            main.upsert_all_email_body_cache("user@example.com", main.EmailDetailsResponse(
                message_id="INBOX-1",
                subject="Cached",
                from_email="sender@example.com",
                to_email="user@example.com",
                date="2026-01-01T08:00:00",
                body_plain="cached body",
            ))
            main.get_graph_email_details = fail_graph_fetch
            main.load_account_classifications_data = lambda: {
                "categories": {},
                "tags": {
                    "welcome": {"name_zh": "欢迎邮件", "name_en": "Welcome"}
                },
            }
            main.load_email_tags_data = lambda: {
                "emails": {
                    "user@example.com": {
                        "INBOX-1": ["welcome"]
                    }
                }
            }

            details = asyncio.run(main.get_email_details(
                main.AccountCredentials(
                    email="user@example.com",
                    refresh_token="refresh",
                    client_id="client",
                    auth_method="graph",
                ),
                "INBOX-1",
            ))
        finally:
            main.ALL_EMAIL_SQLITE_FILE = original_sqlite_file
            main.get_graph_email_details = original_get_graph_email_details
            main.load_account_classifications_data = original_load_account_classifications_data
            main.load_email_tags_data = original_load_email_tags_data
            temp_dir.cleanup()

        self.assertEqual("cached body", details.body_plain)
        self.assertEqual(["welcome"], details.tag_keys)
        self.assertEqual("欢迎邮件", details.tag_details[0].name_zh)


class AllEmailSyncSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_site_settings_file = main.SITE_SETTINGS_FILE
        main.SITE_SETTINGS_FILE = Path(self.temp_dir.name) / "site_settings.json"

    def tearDown(self):
        if hasattr(main, "reset_all_email_sqlite_refresh_status_for_tests"):
            main.reset_all_email_sqlite_refresh_status_for_tests()
        main.SITE_SETTINGS_FILE = self.original_site_settings_file
        self.temp_dir.cleanup()

    def make_site_settings(self, **overrides):
        payload = {
            "home_title": "Microsoft-Email-Manager",
            "home_intro": "Mail manager",
            "admin_login_path": "/admin",
            "share_domain_enabled": False,
            "share_domain": "",
            "share_domain_turnstile_enabled": False,
            "share_domain_turnstile_site_key": "",
            "share_domain_turnstile_secret_key": "",
            "turnstile_site_key": "",
            "turnstile_secret_key": "",
            "turnstile_enabled_for_admin_login": False,
            "turnstile_enabled_for_public_access": False,
        }
        payload.update(overrides)
        return payload

    def test_site_settings_default_enable_all_email_auto_sync(self):
        settings = main.load_site_settings()

        self.assertTrue(settings["all_email_sqlite_auto_sync_enabled"])
        self.assertEqual(5, settings["all_email_sqlite_sync_concurrency"])
        self.assertEqual(30, settings["all_email_sqlite_auto_sync_interval_minutes"])

    def test_site_settings_persist_all_email_sync_options(self):
        main.save_site_settings(self.make_site_settings(
            all_email_sqlite_auto_sync_enabled=False,
            all_email_sqlite_sync_concurrency=12,
            all_email_sqlite_auto_sync_interval_minutes=30,
        ))

        settings = main.load_site_settings()

        self.assertFalse(settings["all_email_sqlite_auto_sync_enabled"])
        self.assertEqual(12, settings["all_email_sqlite_sync_concurrency"])
        self.assertEqual(30, settings["all_email_sqlite_auto_sync_interval_minutes"])

    def test_auto_sync_once_uses_configured_concurrency(self):
        original_start_refresh = main.start_all_email_sqlite_refresh
        calls = []

        async def fake_start_refresh(concurrency=None, trigger="manual", retention_days=None):
            calls.append((concurrency, trigger))
            return main.AllEmailSqliteRefreshStatusResponse(
                task_id="task",
                running=True,
                total_accounts=1,
                concurrency=concurrency,
            )

        try:
            main.save_site_settings(self.make_site_settings(
                all_email_sqlite_auto_sync_enabled=True,
                all_email_sqlite_sync_concurrency=9,
                all_email_sqlite_auto_sync_interval_minutes=15,
            ))
            main.start_all_email_sqlite_refresh = fake_start_refresh

            status = asyncio.run(main.run_all_email_sqlite_auto_sync_once())
        finally:
            main.start_all_email_sqlite_refresh = original_start_refresh

        self.assertEqual([(9, "scheduled")], calls)
        self.assertTrue(status.running)

    def test_auto_sync_once_skips_when_disabled(self):
        original_start_refresh = main.start_all_email_sqlite_refresh
        calls = []

        async def fake_start_refresh(concurrency=None, trigger="manual"):
            calls.append((concurrency, trigger))
            return main.AllEmailSqliteRefreshStatusResponse()

        try:
            main.save_site_settings(self.make_site_settings(
                all_email_sqlite_auto_sync_enabled=False,
                all_email_sqlite_sync_concurrency=9,
            ))
            main.start_all_email_sqlite_refresh = fake_start_refresh

            status = asyncio.run(main.run_all_email_sqlite_auto_sync_once())
        finally:
            main.start_all_email_sqlite_refresh = original_start_refresh

        self.assertIsNone(status)
        self.assertEqual([], calls)


class StaticConfigurationTest(unittest.TestCase):
    def test_frontend_all_email_sync_status_fallback_matches_backend_default(self):
        html = (PROJECT_ROOT / "static/index.html").read_text(encoding="utf-8")

        self.assertIn(
            "const allEmailSyncInterval = Number(config.all_email_sqlite_auto_sync_interval_minutes || 30);",
            html,
        )

    def test_dockerfile_healthcheck_uses_auth_state_endpoint(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("http://localhost:8000/api/auth/state", dockerfile)
        self.assertNotIn("requests.get('http://localhost:8000/api')", dockerfile)


if __name__ == "__main__":
    unittest.main()
