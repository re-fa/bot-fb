# bot-fb

A folder-driven Telegram bot. You build the whole bot, its menus, pages, files and link buttons, just by arranging folders and files on disk. No code changes needed.

Drop a folder in, and it becomes a button. Put a `main.md` in it, and it becomes the page text. Put a PDF next to it, and users receive the PDF when they open the page.

The whole bot is a single file (`bot.py`) built on [python-telegram-bot](https://python-telegram-bot.org/) and SQLite.

## How it works

The content lives in a folder named `start/` next to `bot.py`.

- **Every folder is a page.**
- **Every subfolder is a button** on its parent's page.
- **Files in a folder are sent** to the user when they open that page.
- Navigation edits the same message, so the chat does not fill up with menus.
- Anything a user sends to the bot (text, a photo, a sticker, an unknown command) shows the main menu, exactly like `/start`.
- Content is read from disk on every request, so you can add, rename or edit folders and files while the bot is running. No restart needed.

### Example

```
start/
├── main.md                  ← welcome text (root page)
├── Math/
│   ├── main.md              ← text of the Math page
│   ├── main.png             ← image shown above the text
│   ├── Chapter 1/
│   │   ├── lesson.pdf
│   │   └── explanation.mp4
│   └── Chapter 2/
│       └── notes.pdf
├── Physics/
│   ├── formulas.pdf         ← no main.md / main.png: files are sent, menu message stays
│   └── lab.mp3
└── Official channel/
    └── link.txt             ← contains https://t.me/example
```

What the user sees:

1. `/start` shows the root text with buttons: **Math**, **Physics**, **Official channel**.
2. **Math** opens the Math page (image + text) with buttons **Chapter 1**, **Chapter 2** and **Back**.
3. **Physics** has no text or image, so the menu message is left untouched and the two files are sent as separate messages.
4. **Official channel** is a link button. Pressing it opens the URL directly and does not enter the folder.

## Special files

| File | What it does |
| --- | --- |
| `main.md` | Text of the page. Plain text (it is HTML-escaped, so no formatting). `{name}` is replaced with a mention of the user. |
| `main.png` / `main.jpg` / `main.jpeg` / `main.webp` | Main image of the page. The text is used as its caption. |
| `link.txt` | Turns the **folder itself** into a URL button. The first line must start with `http://` or `https://`. The folder's content is not reachable when it has a valid `link.txt`. If the link is invalid, the folder behaves as a normal folder. |

None of these files are ever sent to users as attachments.

### Page rules

- A folder with subfolders but **no** `main.md` shows the default text `اختر من القائمة:` ("Choose from the menu"). Change it with `DEFAULT_MENU_TEXT` in `bot.py`.
- A folder with **no** `main.md`, **no** `main.png` and **no** subfolders does not touch the menu message. Its files are just sent.
- A **Back** button is added to every page except the root.
- Buttons are sorted naturally (`1`, `2`, `10`, not `1`, `10`, `2`).
- Hidden files and folders (names starting with `.`) and symlinks are ignored.
- With an image, the text is used as the caption if it is 1000 characters or fewer. Longer text is sent as a separate message with the buttons. Text is truncated at 4000 characters.

## How files are sent

Every other file in a folder is sent, whatever its extension.

| Extension | Sent as |
| --- | --- |
| `.mp4` `.mov` `.mkv` `.avi` `.webm` | Video |
| `.mp3` `.ogg` `.wav` `.m4a` `.opus` | Audio |
| `.png` `.jpg` `.jpeg` `.webp` `.bmp` | Photo |
| `.pdf` and everything else (including no extension) | Document |

Notes:

- If Telegram rejects a video, audio or photo (unsupported format, bad dimensions), the file is sent again as a document.
- Photos larger than 10 MB, and GIFs (to keep the animation), are sent as documents.
- Telegram limits bot uploads to **50 MB** per file. Larger files are skipped and the user gets a notice.
- After the first upload, the file's Telegram `file_id` is cached in the database, so later sends are instant and use no upload bandwidth. If you replace the file, it is uploaded again automatically.

## Setup

### Requirements

- Python 3.9 or newer
- A bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID (for admin commands). You can get it from a bot such as [@userinfobot](https://t.me/userinfobot).

### 1. Get the files

Put `bot.py` in a folder of your own, for example `bot-fb/`. The bot finds everything relative to the location of `bot.py`, so the folder can live anywhere.

### 2. Install dependencies

On Linux, macOS or Windows:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install python-telegram-bot python-dotenv
```

On Termux (Android):

```bash
pkg update
pkg install python
pip install python-telegram-bot python-dotenv
```

### 3. Create the `.env` file

Create a file named `.env` next to `bot.py`:

```
BOT_TOKEN=123456:your-token-here
ADMIN_ID=123456789
```

Never commit this file or share the token. If it leaks, revoke it with `/revoke` in BotFather.

### 4. Create your content

Create the `start/` folder next to `bot.py` (the bot also creates it on first run) and fill it as described above.

### 5. Run

```bash
python bot.py
```

When you see `البوت يعمل...` ("the bot is running"), open your bot in Telegram and send `/start`.

To keep it running after you close the terminal, use `tmux`, `screen`, `nohup python bot.py &`, or a systemd service.

## Admin commands

Only the user whose ID is set as `ADMIN_ID` can use these.

| Command | What it does |
| --- | --- |
| `/stats` | Total users, active today, interactions today, interactions in the last 7 days, latest user. |
| `/all` | Broadcast. Send `/all`, then send any message (text, photo, file...). The bot shows a preview and asks for confirmation before sending it to every user. Users who blocked the bot are skipped and counted. |
| `/cancel` | Cancels a broadcast in progress. |

## Data

The bot creates `database.sqlite3` automatically on first run. It contains:

- `users`: Telegram ID, username, first name, join date, last seen, interaction count
- `interactions`: a log of every `/start` and button press
- `file_cache`: cached Telegram `file_id`s

This file contains personal data about your users. Do not publish it. The repository's `.gitignore` excludes it, together with `.env` and `start/`.

## Design notes

- **Concurrent:** updates are handled concurrently, so one user downloading a large file does not block the others. Each user's own clicks are processed in order.
- **Safe navigation:** buttons carry short hashed IDs, never file paths, so a button cannot point outside `start/`. IDs are derived from folder paths, so they keep working after a restart.
- **Single file:** everything is in `bot.py`, split into sections: settings, database, file cache, paths and navigation, text, sending, commands, broadcast, startup.

## Project layout

```
bot-fb/
├── bot.py
├── .gitignore
├── README.md
│
│   created when you set it up / run it (not in the repository):
├── .env
├── start/
└── database.sqlite3
```
