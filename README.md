# FileLinker Bot 🤖

FileLinker Bot is a feature-rich Telegram bot built using **Python**, **Pyrogram**, and **Flask**. It converts files (documents, videos, photos, audio) into permanent, shareable deep-links with support for multi-file bundles, custom thumbnails, custom force-join channels, inline search, admin controls, and group moderation.

---

## 🚀 Features

- 📄 **Single File to Link**: Convert any uploaded file into a permanent shareable Telegram link.
- 📦 **Multi-File Bundling**: Combine multiple files into a single shareable link.
- 🖼️ **Custom Thumbnails**: Set custom thumbnails for single files or bundles.
- 🔒 **Force Join Channels**: Enforce users to join specific channels before retrieving files.
- 🔍 **Inline Search**: Search and share your uploaded files/bundles directly in any Telegram chat.
- ⏱️ **Auto-Deletion**: Auto-deletes sent files after 60 minutes to protect storage and privacy.
- 👑 **Admin Controls**: Stat tracking, public/private upload mode toggle, and message broadcasting.
- 🛡️ **Group Moderation**: Anti-link filter, bad-word filter, user warnings, temporary muting, and kicking.

---

## 📜 Complete Commands List

### 👤 User Commands

| Command | Description |
| :--- | :--- |
| `/start` | Starts the bot and displays the main menu, or retrieves a file/bundle when passed a parameter (e.g. `/start <file_id>`). |
| `/help` | Shows the usage guide and help menu. |
| `/create_link [@channel] [title]` | Prepares the bot to accept a file for link generation. Optionally specify a custom force-join channel and title. |
| `/multi_link [@channel] [title]` | Starts multi-file bundle mode. Forward multiple files and finish with `/done`. Optionally specify force-join channel and title. |
| `/done` | Finalizes the current multi-file bundle and generates its shareable link. |
| `/set_thumbnail` | Reply to a photo with this command to set it as a thumbnail for future uploads or bundles. |
| `/cancel_thumbnail` | Cancels and removes the current custom thumbnail setting. |
| `/myfiles` | Displays your last 10 uploaded single files and multi-file bundles. |
| `/delete <file_id>` | Prompts confirmation to permanently delete a file or bundle from the database and log channel. |

---

### 👑 Admin Commands

| Command | Description |
| :--- | :--- |
| `/admin` | Opens the Admin Panel dashboard to manage bot settings, stats, and broadcasts. |
| `/stats` | Views detailed bot statistics including user counts, active users, total files, and breakdown by file types. |
| `/broadcast` | Broadcasts a text message or replied media/message to all registered bot users. |

---

### 🛡️ Group Moderation Commands

| Command | Description |
| :--- | :--- |
| `/warn` | (Admin) Reply to a user in a group to issue a warning. Reaching maximum warnings triggers a 24-hour mute. |
| `/mute <duration>` | (Admin) Reply to a user to mute them for a specified duration (e.g., `/mute 30m`, `/mute 2h`, `/mute 1d`). |
| `/unmute` | (Admin) Reply to a user to remove their mute status. |
| `/kick` | (Admin) Reply to a user to kick them from the group. |

---

## 🔍 Inline Search Usage

You can search and share your uploaded files anywhere on Telegram using inline mode:

```text
@bot_username <file_name>
```

---

## ⚙️ Environment Variables Configuration

Create a `.env` file or configure environment variables in your deployment platform:

```env
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
LOG_CHANNEL=-1001234567890
GROUP_LOG_CHANNEL=-1001234567890
OWNER_ID=7524032836
ADMINS=7524032836,123456789
FORCE_CHANNELS=channel1,channel2
BADWORDS=bsdk,bc,mc,laura,land,fuck,bitch
MAX_WARNINGS=3
PORT=8080
```

---

## 🛠️ Installation & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the bot:**
   ```bash
   python3 bot.py
   ```
