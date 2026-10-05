import os, unittest
SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guardian_bot.py")).read()


class Shadowban(unittest.TestCase):
    def test_admins_mods_bots_and_self_are_protected(self):
        i = SRC.index("async def gshadowban")
        body = SRC[i:SRC.index("async def gunshadowban")]
        for needle in ("target.is_bot", "update.effective_user.id", "ChatMemberStatus.ADMINISTRATOR", "_is_guardian_mod("):
            self.assertIn(needle, body)
        self.assertLess(body.index("_is_guardian_mod("), body.index("INSERT OR REPLACE INTO shadowbanned"))


if __name__ == "__main__":
    unittest.main()
