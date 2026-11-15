#!/usr/bin/env python3
"""Analyze an exported WhatsApp chat (.txt) and report statistics.

Counts messages per participant, activity over time, media and deleted
messages, top words, emoji and links. Can render a self-contained HTML
report that works fully offline.

Usage:
    python3 whatsapp_chat_analyzer.py chat.txt
    python3 whatsapp_chat_analyzer.py chat.txt --html report.html
    python3 whatsapp_chat_analyzer.py chat.txt --json --top 20

Supports the common Android and iOS export formats, 12-hour and 24-hour
clocks, and multi-line messages. Standard library only.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

MEDIA_MARKERS = frozenset({
    "<media omitted>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "document omitted",
    "sticker omitted",
    "gif omitted",
    "contact card omitted",
    "voice message omitted",
})

DELETED_MARKERS = frozenset({
    "this message was deleted",
    "you deleted this message",
})

SYSTEM_HINTS = (
    "end-to-end encrypted",
    "created group",
    "changed the subject",
    "changed this group",
    "changed the group",
    "added",
    "removed",
    "left",
    "joined using",
    "changed their phone number",
    "security code changed",
    "pinned a message",
    "changed the group description",
    "disappearing messages",
    "became an admin",
    "video call",
    "voice call",
    "missed voice call",
    "missed video call",
)

WORD_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0600-\u06FF][\w\u00C0-\u024F\u0600-\u06FF'\u064B-\u0652-]*", re.UNICODE)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
# Approximate emoji ranges (BMP symbols + supplementary planes).
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")

EN_STOPWORDS = frozenset("""
a about after again all am an and any are aren't as at be because been before
being below between both but by can can't cannot could couldn't did didn't do
does doesn't doing don't down during each few for from further get got had
hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
herself him himself his how how's i i'd i'll i'm i've if in into is isn't it
it's its itself just like me more most mustn't my myself no nor not of off on
once only or other our ours ourselves out over own same shan't she she'd
she'll she's should shouldn't so some such than that that's the their theirs
them themselves then there there's these they they'd they'll they're they've
this those through to too under until up very was wasn't we we'd we'll we're
we've were weren't what what's when when's where where's which while who who's
whom why why's will with won't would wouldn't you you'd you'll you're you've
your yours yourself yourselves ok okay yeah yes nah huh hmm lol haha hahaha
""".split())

AR_STOPWORDS = frozenset("""
في من على إلى عن أن إن كان كانت التي الذي الذين هذا هذه ذلك تلك هو هي نحن أنا
انت أنت أنتم هم هن ما لا لم لن لقد قد كل بعد قبل عند أو ام مع اذا إذا ثم هنا
هناك بين فوق تحت امام وراء لكن ليس أي اي كذلك أيضا فقط جدا حيث كيف لماذا متى
ماذا و يا الا الى علي عليه الي يعني طيب اوكي تمام ماشي اه ايوه
""".split())

STOPWORDS = EN_STOPWORDS | AR_STOPWORDS

# Header patterns for the two common export styles. Named groups:
# d (day or month), m (month or day), y, t (hour), mi (minute), s (second),
# ap (AM/PM), rest (everything after the header).
HEADER_RES = (
    # iOS: [31/12/2020, 23:59:59] Sender: text
    re.compile(
        r"^\[(?P<d>\d{1,2})[./\u2010-](?P<m>\d{1,2})[./\u2010-](?P<y>\d{2,4}),"
        r"\s+(?P<t>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
        r"(?:\s*(?P<ap>[AP]\.?M\.?))?\]\s*(?P<rest>.*)$",
        re.IGNORECASE,
    ),
    # Android: 31/12/2020, 23:59 - Sender: text
    re.compile(
        r"^(?P<d>\d{1,2})[./\u2010-](?P<m>\d{1,2})[./\u2010-](?P<y>\d{2,4}),"
        r"\s+(?P<t>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
        r"(?:\s*(?P<ap>[AP]\.?M\.?))?\s+\u200e?-\s+(?P<rest>.*)$",
        re.IGNORECASE,
    ),
)

MAX_SENDER_LEN = 80


@dataclass
class Message:
    when: datetime
    sender: Optional[str]  # None for system lines
    text: str

    @property
    def is_media(self) -> bool:
        return self.text.strip().lower() in MEDIA_MARKERS

    @property
    def is_deleted(self) -> bool:
        return self.text.strip().lower() in DELETED_MARKERS

    @property
    def is_system(self) -> bool:
        return self.sender is None


def _match_header(line: str):
    for rx in HEADER_RES:
        m = rx.match(line)
        if m:
            return m
    return None


def detect_dayfirst(lines) -> bool:
    """Decide day-first vs month-first from the dates present in the file.

    A value above 12 in the first position forces day-first, and in the
    second position forces month-first. When every date is ambiguous the
    export locale is unknowable; default to day-first, which covers most
    locales outside the US.
    """
    saw_day_gt_12 = False
    saw_month_gt_12 = False
    for line in lines:
        m = _match_header(line)
        if not m:
            continue
        first, second = int(m.group("d")), int(m.group("m"))
        if first > 12:
            saw_day_gt_12 = True
        if second > 12:
            saw_month_gt_12 = True
    if saw_day_gt_12 and not saw_month_gt_12:
        return True
    if saw_month_gt_12 and not saw_day_gt_12:
        return False
    return True  # ambiguous or conflicting: day-first default


def _to_datetime(groups, dayfirst: bool) -> datetime:
    first, second = int(groups.group("d")), int(groups.group("m"))
    day, month = (first, second) if dayfirst else (second, first)
    year = int(groups.group("y"))
    if year < 100:
        year += 2000
    hour = int(groups.group("t"))
    ap = groups.group("ap")
    if ap:
        ap = ap.replace(".", "").upper()
        if ap == "PM" and hour != 12:
            hour += 12
        elif ap == "AM" and hour == 12:
            hour = 0
    minute = int(groups.group("mi"))
    second_ = int(groups.group("s") or 0)
    return datetime(year, month, day, hour, minute, second_)


def _looks_like_system(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in SYSTEM_HINTS)


def parse_chat(text: str, dayfirst: Optional[bool] = None) -> list:
    """Parse export text into a list of Message objects."""
    lines = text.splitlines()
    if dayfirst is None:
        dayfirst = detect_dayfirst(lines)

    messages: list[Message] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        header = _match_header(line)
        if header:
            try:
                when = _to_datetime(header, dayfirst)
            except ValueError:
                # Not a real header after all; treat as continuation.
                if messages:
                    messages[-1].text += "\n" + line
                continue
            rest = header.group("rest")
            sender: Optional[str] = None
            body = rest
            if ": " in rest:
                maybe_sender, maybe_body = rest.split(": ", 1)
                if len(maybe_sender) <= MAX_SENDER_LEN and not _looks_like_system(rest):
                    sender = maybe_sender.strip()
                    body = maybe_body
            if sender is None and _looks_like_system(rest):
                sender = None
                body = rest
            messages.append(Message(when=when, sender=sender, text=body))
        elif messages:
            messages[-1].text += "\n" + line
        # Lines before the first header are ignored (rare export preamble).
    return messages


def analyze(messages: list, top: int = 10) -> dict:
    """Compute statistics for parsed messages."""
    user_msgs = [m for m in messages if not m.is_system]
    system_count = len(messages) - len(user_msgs)

    per_sender = Counter()
    words_per_sender = Counter()
    media_per_sender = Counter()
    per_hour = Counter()
    per_day = Counter()
    per_month = Counter()
    word_counter = Counter()
    emoji_counter = Counter()
    domain_counter = Counter()
    media_total = 0
    deleted_total = 0
    link_total = 0
    longest = None

    for m in user_msgs:
        sender = m.sender or "(unknown)"
        per_sender[sender] += 1
        per_hour[m.when.hour] += 1
        per_day[m.when.date().isoformat()] += 1
        per_month[m.when.strftime("%Y-%m")] += 1
        if m.is_media:
            media_total += 1
            media_per_sender[sender] += 1
            continue
        if m.is_deleted:
            deleted_total += 1
            continue
        words = [w.lower() for w in WORD_RE.findall(m.text)]
        words_per_sender[sender] += len(words)
        for w in words:
            if len(w) >= 3 and w not in STOPWORDS:
                word_counter[w] += 1
        for e in EMOJI_RE.findall(m.text):
            if e not in ("\uFE0F", "\u200D"):
                emoji_counter[e] += 1
        for u in URL_RE.findall(m.text):
            link_total += 1
            host = urlparse(u).netloc.lower().lstrip("www.")
            if host:
                domain_counter[host] += 1
        n_words = len(words)
        if longest is None or n_words > longest[1]:
            longest = (sender, n_words, m.text[:140])

    dates = sorted(per_day)
    busiest_day = per_day.most_common(1)[0] if per_day else (None, 0)
    busiest_hour = per_hour.most_common(1)[0] if per_hour else (None, 0)

    senders = []
    for name, count in per_sender.most_common():
        senders.append({
            "sender": name,
            "messages": count,
            "words": words_per_sender.get(name, 0),
            "media": media_per_sender.get(name, 0),
            "share": round(100.0 * count / len(user_msgs), 1) if user_msgs else 0.0,
        })

    return {
        "total_messages": len(user_msgs),
        "system_messages": system_count,
        "participants": len(per_sender),
        "media_messages": media_total,
        "deleted_messages": deleted_total,
        "links_shared": link_total,
        "first_message": dates[0] if dates else None,
        "last_message": dates[-1] if dates else None,
        "active_days": len(per_day),
        "busiest_day": {"date": busiest_day[0], "messages": busiest_day[1]},
        "busiest_hour": {"hour": busiest_hour[0], "messages": busiest_hour[1]},
        "avg_words_per_message": (
            round(sum(words_per_sender.values()) / len(user_msgs), 2) if user_msgs else 0.0
        ),
        "longest_message": (
            {"sender": longest[0], "words": longest[1], "preview": longest[2]}
            if longest else None
        ),
        "per_sender": senders,
        "per_month": dict(sorted(per_month.items())),
        "per_hour": {str(h): per_hour.get(h, 0) for h in range(24)},
        "top_words": word_counter.most_common(top),
        "top_emoji": emoji_counter.most_common(top),
        "top_domains": domain_counter.most_common(top),
    }


def _fmt_hour(h: Optional[int]) -> str:
    if h is None:
        return "-"
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12} {suffix}"


def render_text(stats: dict, top: int) -> str:
    out = []
    out.append("WhatsApp chat analysis")
    out.append("=" * 40)
    out.append(f"Messages: {stats['total_messages']} "
               f"(+{stats['system_messages']} system)")
    out.append(f"Participants: {stats['participants']}")
    if stats["first_message"]:
        out.append(f"Span: {stats['first_message']} to {stats['last_message']} "
                   f"({stats['active_days']} active days)")
    out.append(f"Media: {stats['media_messages']}  "
               f"Deleted: {stats['deleted_messages']}  "
               f"Links: {stats['links_shared']}")
    if stats["busiest_day"]["date"]:
        out.append(f"Busiest day: {stats['busiest_day']['date']} "
                   f"({stats['busiest_day']['messages']} messages)")
    if stats["busiest_hour"]["hour"] is not None:
        out.append(f"Busiest hour: {_fmt_hour(int(stats['busiest_hour']['hour']))} "
                   f"({stats['busiest_hour']['messages']} messages)")
    out.append(f"Avg words per message: {stats['avg_words_per_message']}")
    out.append("")
    out.append("Per participant:")
    for s in stats["per_sender"]:
        out.append(f"  {s['sender']}: {s['messages']} msgs, {s['words']} words, "
                   f"{s['media']} media ({s['share']}%)")
    if stats["top_words"]:
        out.append("")
        out.append("Top words: " + ", ".join(f"{w} ({c})" for w, c in stats["top_words"]))
    if stats["top_emoji"]:
        out.append("Top emoji: " + " ".join(f"{e} {c}" for e, c in stats["top_emoji"]))
    if stats["top_domains"]:
        out.append("Top link domains: " + ", ".join(f"{d} ({c})" for d, c in stats["top_domains"]))
    if stats["longest_message"]:
        lm = stats["longest_message"]
        out.append(f"Longest message: {lm['words']} words by {lm['sender']}")
    return "\n".join(out)


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _bars(rows, label_key, value_key, max_width=100) -> str:
    if not rows:
        return "<p class='empty'>none</p>"
    peak = max(r[1] for r in rows) or 1
    parts = []
    for label, value in rows:
        width = max(1, round(value / peak * max_width))
        parts.append(
            "<tr>"
            f"<td class='lbl'>{_esc(label)}</td>"
            f"<td class='bar'><div style='width:{width}%'></div></td>"
            f"<td class='num'>{_esc(value)}</td>"
            "</tr>"
        )
    return "<table class='bars'>" + "".join(parts) + "</table>"


def render_html(stats: dict, top: int) -> str:
    hour_rows = [(f"{h:02d}:00", int(stats["per_hour"].get(str(h), 0))) for h in range(24)]
    month_rows = [(k, v) for k, v in sorted(stats["per_month"].items())]
    sender_rows = [(s["sender"], s["messages"]) for s in stats["per_sender"]]
    word_rows = [(w, c) for w, c in stats["top_words"]]
    emoji_rows = [(e, c) for e, c in stats["top_emoji"]]
    domain_rows = [(d, c) for d, c in stats["top_domains"]]

    def card(title, value):
        return f"<div class='card'><div class='v'>{_esc(value)}</div><div class='t'>{_esc(title)}</div></div>"

    span = "-"
    if stats["first_message"]:
        span = f"{stats['first_message']} &rarr; {stats['last_message']}"

    sender_table_rows = "".join(
        "<tr>"
        f"<td>{_esc(s['sender'])}</td><td>{s['messages']}</td>"
        f"<td>{s['words']}</td><td>{s['media']}</td><td>{s['share']}%</td>"
        "</tr>"
        for s in stats["per_sender"]
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp chat analysis</title>
<style>
body{{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1c1e21}}
main{{max-width:860px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 10px;color:#333}}
.sub{{color:#667;font-size:14px;margin-bottom:24px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}}
.card{{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:14px 18px;min-width:110px;flex:1}}
.card .v{{font-size:22px;font-weight:600}} .card .t{{font-size:12px;color:#667;margin-top:2px}}
table.bars{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e5e8;border-radius:10px;overflow:hidden}}
table.bars td{{padding:6px 10px;font-size:13px}}
td.lbl{{white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}}
td.bar{{width:55%}} td.bar div{{background:#00a884;height:14px;border-radius:3px;min-width:2px}}
td.num{{text-align:right;color:#667;width:50px}}
table.grid{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e5e8;font-size:13px}}
table.grid th,table.grid td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef0f2}}
table.grid th{{background:#fafbfc;color:#556}}
.empty{{color:#99a;font-size:13px}}
footer{{margin-top:40px;color:#99a;font-size:12px}}
</style></head><body><main>
<h1>WhatsApp chat analysis</h1>
<div class="sub">{_esc(span)} &middot; {_esc(stats['active_days'])} active days</div>
<div class="cards">
{card('messages', stats['total_messages'])}
{card('participants', stats['participants'])}
{card('media', stats['media_messages'])}
{card('deleted', stats['deleted_messages'])}
{card('links', stats['links_shared'])}
{card('avg words / msg', stats['avg_words_per_message'])}
</div>
<h2>Participants</h2>
<table class="grid"><tr><th>Sender</th><th>Messages</th><th>Words</th><th>Media</th><th>Share</th></tr>
{sender_table_rows}</table>
<h2>Messages by month</h2>
{_bars(month_rows, 0, 1)}
<h2>Messages by hour of day</h2>
{_bars(hour_rows, 0, 1)}
<h2>Top words</h2>
{_bars(word_rows, 0, 1)}
<h2>Top emoji</h2>
{_bars(emoji_rows, 0, 1)}
<h2>Top link domains</h2>
{_bars(domain_rows, 0, 1)}
<footer>Generated by whatsapp-chat-analyzer. All data stays local.</footer>
</main></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Analyze an exported WhatsApp chat (.txt).")
    ap.add_argument("chat_file", help="Path to the exported chat .txt file")
    ap.add_argument("--html", metavar="OUT.html", help="Write a self-contained HTML report")
    ap.add_argument("--json", action="store_true", help="Print statistics as JSON")
    ap.add_argument("--top", type=int, default=10, help="Number of top items to show (default 10)")
    order = ap.add_mutually_exclusive_group()
    order.add_argument("--day-first", action="store_true",
                       help="Force day-first dates (dd/mm/yyyy)")
    order.add_argument("--month-first", action="store_true",
                       help="Force month-first dates (mm/dd/yyyy)")
    ap.add_argument("--encoding", default="utf-8-sig",
                    help="File encoding (default utf-8-sig)")
    args = ap.parse_args(argv)

    try:
        with open(args.chat_file, "r", encoding=args.encoding) as fh:
            text = fh.read()
    except OSError as exc:
        print(f"error: cannot read {args.chat_file}: {exc}", file=sys.stderr)
        return 2

    dayfirst = True if args.day_first else (False if args.month_first else None)
    messages = parse_chat(text, dayfirst=dayfirst)
    if not messages:
        print("error: no WhatsApp message lines found. "
              "Export the chat from WhatsApp (More > Export chat > Without media) "
              "and pass the resulting .txt file.", file=sys.stderr)
        return 1

    stats = analyze(messages, top=args.top)

    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(stats, args.top))
        print(f"HTML report written to {args.html}")

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif not args.html:
        print(render_text(stats, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
