import unittest

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


if __name__ == "__main__":
    unittest.main()
