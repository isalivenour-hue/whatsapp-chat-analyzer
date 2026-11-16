import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whatsapp_chat_analyzer import (
    analyze,
    detect_dayfirst,
    main,
    parse_chat,
    render_html,
)

ANDROID_24H = """\
14/11/2026, 09:15 - Alex: Good morning!
14/11/2026, 09:16 - Sara: Morning! Did you see the game yesterday?
14/11/2026, 09:17 - Alex: Yes it was amazing
still thinking about that last goal
14/11/2026, 21:03 - Sara: <Media omitted>
14/11/2026, 21:04 - Sara: This message was deleted
14/11/2026, 21:05 - Alex: check this https://github.com/noah-hayes-dev cool site
15/11/2026, 08:00 - Sara: hello hello hello test test
"""

ANDROID_12H = """\
11/14/2026, 9:15 AM - Alex: hi there
11/14/2026, 9:16 PM - Sara: evening reply
"""

IOS_FMT = """\
[14/11/2026, 09:15:22] Alex: ios hello
[14/11/2026, 21:05:01] Sara: ios reply with emoji 😂😂
"""

SYSTEM_LINES = """\
14/11/2026, 09:00 - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.
14/11/2026, 09:01 - Alex created group "Weekend"
14/11/2026, 09:02 - Alex: first real message
14/11/2026, 09:03 - Sara left
"""


class TestParsing(unittest.TestCase):
    def test_android_24h(self):
        msgs = parse_chat(ANDROID_24H)
        self.assertEqual(len(msgs), 7)
        self.assertEqual(msgs[0].sender, "Alex")
        self.assertEqual(msgs[0].when.hour, 9)
        self.assertEqual(msgs[0].when.day, 14)
        self.assertEqual(msgs[0].when.month, 11)

    def test_multiline_continuation(self):
        msgs = parse_chat(ANDROID_24H)
        self.assertIn("still thinking", msgs[2].text)
        self.assertIn("amazing", msgs[2].text)

    def test_media_and_deleted(self):
        msgs = parse_chat(ANDROID_24H)
        self.assertTrue(msgs[3].is_media)
        self.assertTrue(msgs[4].is_deleted)

    def test_android_12h_ampm(self):
        msgs = parse_chat(ANDROID_12H)
        self.assertEqual(msgs[0].when.hour, 9)
        self.assertEqual(msgs[1].when.hour, 21)

    def test_ios_format(self):
        msgs = parse_chat(IOS_FMT)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].sender, "Alex")
        self.assertEqual(msgs[1].when.hour, 21)
        self.assertEqual(msgs[1].when.second, 1)

    def test_system_messages_have_no_sender(self):
        msgs = parse_chat(SYSTEM_LINES)
        self.assertIsNone(msgs[0].sender)
        self.assertIsNone(msgs[1].sender)
        self.assertEqual(msgs[2].sender, "Alex")
        self.assertIsNone(msgs[3].sender)

    def test_dayfirst_detection(self):
        self.assertTrue(detect_dayfirst(ANDROID_24H.splitlines()))
        # month-first: first value never >12, second value is
        self.assertFalse(detect_dayfirst(ANDROID_12H.splitlines()))
        msgs = parse_chat(ANDROID_12H)
        self.assertEqual(msgs[0].when.month, 11)
        self.assertEqual(msgs[0].when.day, 14)

    def test_forced_month_first(self):
        # Ambiguous date (05/11) resolves differently under each rule.
        ambiguous = "05/11/2026, 10:00 - Alex: hello\n"
        self.assertEqual(parse_chat(ambiguous, dayfirst=True)[0].when.month, 11)
        self.assertEqual(parse_chat(ambiguous, dayfirst=False)[0].when.month, 5)

    def test_invalid_date_falls_back_to_continuation(self):
        # 14 cannot be a month: forced month-first makes the header invalid,
        # so the line merges into the previous message instead of crashing.
        text = "05/11/2026, 10:00 - Alex: first\n14/11/2026, 11:00 - Sara: second\n"
        msgs = parse_chat(text, dayfirst=False)
        self.assertEqual(len(msgs), 1)
        self.assertIn("second", msgs[0].text)


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.stats = analyze(parse_chat(ANDROID_24H), top=5)

    def test_totals(self):
        self.assertEqual(self.stats["total_messages"], 7)
        self.assertEqual(self.stats["participants"], 2)
        self.assertEqual(self.stats["media_messages"], 1)
        self.assertEqual(self.stats["deleted_messages"], 1)
        self.assertEqual(self.stats["links_shared"], 1)

    def test_per_sender(self):
        senders = {s["sender"]: s for s in self.stats["per_sender"]}
        self.assertEqual(senders["Alex"]["messages"], 3)
        self.assertEqual(senders["Sara"]["messages"], 4)
        self.assertEqual(senders["Sara"]["media"], 1)

    def test_time_rollup(self):
        self.assertEqual(self.stats["first_message"], "2026-11-14")
        self.assertEqual(self.stats["last_message"], "2026-11-15")
        self.assertEqual(self.stats["active_days"], 2)
        self.assertEqual(self.stats["busiest_day"]["messages"], 6)

    def test_top_words_skip_stopwords_and_media(self):
        words = dict(self.stats["top_words"])
        self.assertEqual(words.get("hello"), 3)
        self.assertEqual(words.get("test"), 2)
        self.assertNotIn("the", words)

    def test_emoji_and_domains(self):
        stats = analyze(parse_chat(IOS_FMT), top=5)
        emoji = dict(stats["top_emoji"])
        self.assertEqual(emoji.get("😂"), 2)
        domains = dict(self.stats["top_domains"])
        self.assertEqual(domains.get("github.com"), 1)

    def test_system_excluded_from_stats(self):
        stats = analyze(parse_chat(SYSTEM_LINES))
        self.assertEqual(stats["total_messages"], 1)
        self.assertEqual(stats["system_messages"], 3)


class TestRenderAndCli(unittest.TestCase):
    def test_html_escapes_and_renders(self):
        stats = analyze(parse_chat(ANDROID_24H.replace("Alex", "<b>Alex</b>")))
        page = render_html(stats, 5)
        self.assertIn("WhatsApp chat analysis", page)
        self.assertNotIn("<b>Alex</b>", page)
        self.assertIn("&lt;b&gt;Alex&lt;/b&gt;", page)

    def test_cli_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chat.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ANDROID_24H)
            from io import StringIO
            buf = StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                rc = main([path, "--json"])
            finally:
                sys.stdout = old
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["total_messages"], 7)

    def test_cli_html_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = os.path.join(tmp, "chat.txt")
            out = os.path.join(tmp, "report.html")
            with open(chat, "w", encoding="utf-8") as fh:
                fh.write(ANDROID_24H)
            rc = main([chat, "--html", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                self.assertIn("Top words", fh.read())

    def test_cli_rejects_non_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "random.txt")
            with open(path, "w") as fh:
                fh.write("just some text\nnot a chat\n")
            self.assertEqual(main([path]), 1)


if __name__ == "__main__":
    unittest.main()
