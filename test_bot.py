import unittest
from pyrogram.enums import ChatMemberStatus
from bot import parse_channel_input

class TestBotChannelParsing(unittest.TestCase):
    def test_numeric_id(self):
        self.assertEqual(parse_channel_input("-1001234567890"), -1001234567890)
        self.assertEqual(parse_channel_input(12345678), 12345678)

    def test_usernames(self):
        self.assertEqual(parse_channel_input("@mychannel"), "mychannel")
        self.assertEqual(parse_channel_input("mychannel"), "mychannel")

    def test_tme_urls(self):
        self.assertEqual(parse_channel_input("https://t.me/mychannel"), "mychannel")
        self.assertEqual(parse_channel_input("https://t.me/mychannel/"), "mychannel")
        self.assertEqual(parse_channel_input("http://telegram.me/mychannel"), "mychannel")
        self.assertEqual(parse_channel_input("t.me/mychannel"), "mychannel")

    def test_invite_links(self):
        self.assertEqual(parse_channel_input("https://t.me/+AbCdEfGh123"), "https://t.me/+AbCdEfGh123")
        self.assertEqual(parse_channel_input("http://t.me/joinchat/AbCdEfGh123"), "http://t.me/joinchat/AbCdEfGh123")
        self.assertEqual(parse_channel_input("t.me/+AbCdEfGh123"), "https://t.me/+AbCdEfGh123")
        self.assertEqual(parse_channel_input("+AbCdEfGh123"), "https://t.me/+AbCdEfGh123")
        self.assertEqual(parse_channel_input("joinchat/AbCdEfGh123"), "https://t.me/joinchat/AbCdEfGh123")

class TestMemberStatusCheck(unittest.TestCase):
    def test_chat_member_status_enum(self):
        class MockMember:
            def __init__(self, status):
                self.status = status

        admin_member = MockMember(ChatMemberStatus.ADMINISTRATOR)
        owner_member = MockMember(ChatMemberStatus.OWNER)
        regular_member = MockMember(ChatMemberStatus.MEMBER)

        for m in [admin_member, owner_member]:
            status_val = m.status.value if hasattr(m.status, "value") else str(m.status)
            self.assertIn(status_val, ["administrator", "owner"])

        status_val_reg = regular_member.status.value if hasattr(regular_member.status, "value") else str(regular_member.status)
        self.assertNotIn(status_val_reg, ["administrator", "owner"])

if __name__ == "__main__":
    unittest.main()
