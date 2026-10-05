import asyncio, os, sys, unittest
from types import SimpleNamespace as NS
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def src():
    return open(os.path.join(HERE, "bot.py")).read()


def run_check(chat_type, status=None, operator=False, channel=False, boom=False):
    s = src()
    i = s.index("async def _feed_admin_ok"); j = s.index("async def setfeed_cmd")
    replies = []

    async def reply_text(t):
        replies.append(t)

    async def get_chat_member(cid, uid):
        if boom:
            raise RuntimeError("x")
        return NS(status=status)

    ns = {"Update": object, "ContextTypes": NS(DEFAULT_TYPE=object), "logger": mock.Mock(), "_is_operator": lambda uid: operator}
    exec(s[i:j], ns)
    upd = NS(channel_post=object() if channel else None, effective_chat=NS(type=chat_type, id=5),
             effective_user=None if channel else NS(id=9), effective_message=NS(reply_text=reply_text))
    ctx = NS(bot=NS(get_chat_member=get_chat_member))
    return asyncio.run(ns["_feed_admin_ok"](upd, ctx)), replies


class FeedAdmin(unittest.TestCase):
    def test_channel_post_ok(self):
        self.assertTrue(run_check("channel", channel=True)[0])

    def test_group_member_refused(self):
        ok, r = run_check("supergroup", status="member")
        self.assertFalse(ok); self.assertTrue(r)

    def test_group_admin_and_creator_ok(self):
        self.assertTrue(run_check("group", status="administrator")[0])
        self.assertTrue(run_check("group", status="creator")[0])

    def test_operator_ok_and_lookup_failure_refuses(self):
        self.assertTrue(run_check("group", status="member", operator=True)[0])
        self.assertFalse(run_check("group", boom=True)[0])

    def test_all_three_commands_check(self):
        s = src()
        for name in ("setfeed_cmd", "feedmin_cmd", "unsetfeed_cmd"):
            i = s.index(f"async def {name}")
            self.assertIn("_feed_admin_ok(update, context)", s[i:i + 1400], name)


if __name__ == "__main__":
    unittest.main()
