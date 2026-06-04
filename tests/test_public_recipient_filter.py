import asyncio
import unittest
from types import SimpleNamespace

import main


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


if __name__ == "__main__":
    unittest.main()
