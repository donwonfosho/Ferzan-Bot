import os, unittest

SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.py"), encoding="utf-8").read()


class FirstWelcome(unittest.TestCase):
    def test_first_screen_says_the_bot_holds_the_key(self):
        i = SRC.index("Welcome to Ferzan")
        seg = SRC[i:i + 900]
        self.assertIn("holds its key", seg)
        self.assertIn("keep only trading funds", seg)
        self.assertIn("19 chains", seg)


if __name__ == "__main__":
    unittest.main()
