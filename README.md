# whatsapp-chat-analyzer

Analyze an exported WhatsApp conversation and get statistics and a clean,
self-contained HTML report. Runs entirely on your machine: the chat export
never leaves your computer.

Pure Python, standard library only. No pip install needed.

## What it reports

- Total messages, per participant (with words and media counts)
- Chat span and number of active days
- Activity by month and by hour of day
- Media, deleted messages and shared links (with top link domains)
- Top words (with English and Arabic stop words filtered) and top emoji
- Longest message, average words per message

## Usage

Export a chat from WhatsApp first:
**Chat screen → ⋮ (or contact name) → More → Export chat → Without media**.
This gives you a `.txt` file. Then:

```bash
# Terminal summary
python3 whatsapp_chat_analyzer.py chat.txt

# Full HTML report
python3 whatsapp_chat_analyzer.py chat.txt --html report.html

# JSON for your own tooling
python3 whatsapp_chat_analyzer.py chat.txt --json --top 20
```

### Date formats

The tool auto-detects day-first (`31/12/2026`) vs month-first (`12/31/2026`)
dates from the values in your file. If auto-detection picks the wrong one
(only possible when every date is ambiguous), force it:

```bash
python3 whatsapp_chat_analyzer.py chat.txt --day-first     # dd/mm/yyyy
python3 whatsapp_chat_analyzer.py chat.txt --month-first   # mm/dd/yyyy
```

## Supported export formats

- Android: `14/11/2026, 21:05 - Name: message` (24-hour and AM/PM clocks)
- iOS: `[14/11/2026, 21:05:01] Name: message`
- Multi-line messages, media placeholders (`<Media omitted>`,
  `image omitted`, ...), deleted messages and system lines are handled.

Group chats and one-to-one chats both work.

## Privacy

Everything runs locally with the Python standard library. No network calls,
no uploads, no telemetry. The HTML report is a static file you can delete
when you are done.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT - see [LICENSE](LICENSE).
