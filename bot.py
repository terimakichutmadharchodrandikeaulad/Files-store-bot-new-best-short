import os
import logging
import random
import string
import time 
import asyncio
import urllib.parse
import json
import re
from threading import Lock, Thread
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import UserNotParticipant, ChatAdminRequired
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
    CallbackQuery, InlineQueryResultArticle,
    InputTextMessageContent, ChatPermissions
)
from flask import Flask

# --- Small Caps Font Utility ---
SMALL_CAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘꞯʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘꞯʀꜱᴛᴜᴠᴡxʏᴢ"
)

def to_small_caps(text: str) -> str:
    """Converts regular text to Small Caps font while preserving markdown, links, codes, and commands."""
    if not text:
        return ""

    # Split by inline code, bold, links, commands, and numbers so formatting isn't broken
    tokens = re.split(r'(`[^`]+`|\*\*[^*]+\*\*|http[s]?://\S+|@\w+|/\w+)', text)
    result = []

    for token in tokens:
        if token.startswith('`') or token.startswith('**') or token.startswith('http') or token.startswith('@') or token.startswith('/'):
            result.append(token)
        else:
            result.append(token.translate(SMALL_CAPS_MAP))

    return "".join(result)

# --- JSON Database Implementation ---
class JsonCollection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def _get_data(self):
        return self.db.data.setdefault(self.name, [])

    def find_one(self, filter_dict):
        with self.db.lock:
            data = self._get_data()
            for doc in data:
                if self._match(doc, filter_dict):
                    return self._copy_doc(doc)
            return None

    def find(self, filter_dict=None):
        with self.db.lock:
            data = self._get_data()
            results = []
            filter_dict = filter_dict or {}
            for doc in data:
                if self._match(doc, filter_dict):
                    results.append(self._copy_doc(doc))
            return JsonCursor(results)

    def insert_one(self, doc):
        with self.db.lock:
            data = self._get_data()
            doc_copy = self._prepare_for_json(doc)
            data.append(doc_copy)
            self.db._save_unlocked()
            return True

    def update_one(self, filter_dict, update_dict, upsert=False):
        with self.db.lock:
            data = self._get_data()
            matched_doc = None
            for doc in data:
                if self._match(doc, filter_dict):
                    matched_doc = doc
                    break

            if matched_doc is not None:
                self._apply_update(matched_doc, update_dict)
                self.db._save_unlocked()
                class UpdateResult:
                    modified_count = 1
                return UpdateResult()
            elif upsert:
                new_doc = {}
                for k, v in filter_dict.items():
                    if not k.startswith("$"):
                        new_doc[k] = v
                if "$set" in update_dict:
                    for k, v in update_dict["$set"].items():
                        new_doc[k] = v
                self._apply_update(new_doc, update_dict)
                prepared_doc = self._prepare_for_json(new_doc)
                data.append(prepared_doc)
                self.db._save_unlocked()
                class UpdateResult:
                    modified_count = 1
                return UpdateResult()
            else:
                class UpdateResult:
                    modified_count = 0
                return UpdateResult()

    def delete_one(self, filter_dict):
        with self.db.lock:
            data = self._get_data()
            for i, doc in enumerate(data):
                if self._match(doc, filter_dict):
                    del data[i]
                    self.db._save_unlocked()
                    return True
            return False

    def count_documents(self, filter_dict=None):
        with self.db.lock:
            data = self._get_data()
            filter_dict = filter_dict or {}
            count = 0
            for doc in data:
                if self._match(doc, filter_dict):
                    count += 1
            return count

    def aggregate(self, pipeline):
        with self.db.lock:
            data = self._get_data()
            for stage in pipeline:
                if "$group" in stage:
                    group_spec = stage["$group"]
                    field = group_spec["_id"].replace("$", "")
                    counts = {}
                    for doc in data:
                        val = doc.get(field)
                        counts[val] = counts.get(val, 0) + 1
                    res = [{"_id": k, "count": v} for k, v in counts.items()]
                    return res
            return []

    def _match(self, doc, filter_dict):
        for key, expected in filter_dict.items():
            val = doc.get(key)
            if isinstance(expected, dict):
                for op, op_val in expected.items():
                    if op == "$gte":
                        if val is None:
                            return False
                        op_val_str = op_val.isoformat() if isinstance(op_val, datetime) else str(op_val)
                        if str(val) < op_val_str:
                            return False
                    elif op == "$regex":
                        if val is None:
                            return False
                        flags = re.IGNORECASE if expected.get("$options") == "i" else 0
                        if not re.search(op_val, str(val), flags):
                            return False
            else:
                if val != expected:
                    return False
        return True

    def _apply_update(self, doc, update_dict):
        if "$set" in update_dict:
            for k, v in update_dict["$set"].items():
                doc[k] = self._prepare_val_for_json(v)
        if "$unset" in update_dict:
            for k in update_dict["$unset"].keys():
                doc.pop(k, None)
        if "$push" in update_dict:
            for k, v in update_dict["$push"].items():
                if k not in doc or not isinstance(doc[k], list):
                    doc[k] = []
                doc[k].append(self._prepare_val_for_json(v))

    def _prepare_val_for_json(self, val):
        if isinstance(val, datetime):
            return val.isoformat()
        if isinstance(val, dict):
            return {k: self._prepare_val_for_json(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._prepare_val_for_json(v) for v in val]
        return val

    def _prepare_for_json(self, doc):
        return {k: self._prepare_val_for_json(v) for k, v in doc.items()}

    def _copy_doc(self, doc):
        doc_copy = json.loads(json.dumps(doc))
        for k, v in doc_copy.items():
            if isinstance(v, str) and (k.endswith("_at") or k in ("last_activity", "last_warned")):
                try:
                    doc_copy[k] = datetime.fromisoformat(v)
                except Exception:
                    pass
        return doc_copy


class JsonCursor:
    def __init__(self, items):
        self.items = items

    def sort(self, key, direction=-1):
        def sort_key(doc):
            val = doc.get(key)
            if isinstance(val, datetime):
                return val.timestamp()
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val).timestamp()
                except Exception:
                    pass
            if val is None:
                return 0
            return val

        self.items.sort(key=sort_key, reverse=(direction == -1))
        return self

    def limit(self, count):
        self.items = self.items[:count]
        return self

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]


class JsonDatabase:
    def __init__(self, filepath="database.json"):
        self.filepath = filepath
        self.lock = Lock()
        self.data = {}
        self._load()

    def _load(self):
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, 'r', encoding='utf-8') as f:
                        self.data = json.load(f)
                except Exception as e:
                    logging.error(f"Error loading JSON DB: {e}")
                    self.data = {}
            else:
                self.data = {}
                self._save_unlocked()

    def _save_unlocked(self):
        try:
            temp_filepath = f"{self.filepath}.tmp"
            with open(temp_filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(temp_filepath, self.filepath)
        except Exception as e:
            logging.error(f"Error saving JSON DB: {e}")

    def __getattr__(self, name):
        return JsonCollection(self, name)

    def __getitem__(self, name):
        return JsonCollection(self, name)

# --- Initialize Flask Web Server ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive! 🚀", 200

def run_flask():
    """Runs the Flask web server."""
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port, threaded=True)

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# --- Load Environment Variables ---
load_dotenv(".env")

# --- Configuration ---
try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
    GROUP_LOG_CHANNEL = int(os.environ.get("GROUP_LOG_CHANNEL", "0"))
    OWNER_ID = int(os.environ.get("OWNER_ID", "7524032836"))
    
    admin_list = os.environ.get("ADMINS", os.environ.get("ADMIN_IDS", str(OWNER_ID))).split(',')
    ADMINS = [OWNER_ID] + [int(admin_id.strip()) for admin_id in admin_list if admin_id.strip() and admin_id.strip().lstrip('-').isdigit()]
    ADMINS = list(set(ADMINS))
    
    FORCE_CHANNELS = [channel.strip() for channel in os.environ.get("FORCE_CHANNELS", "").split(',') if channel.strip()]
    BADWORDS = [word.strip() for word in os.environ.get("BADWORDS", "bsdk,bc,mc,laura,land,bur,Madharchod,kamina,kutta,fuck,bitch,asshole,randi,madarchod").lower().split(',') if word.strip()]
    MAX_WARNINGS = int(os.environ.get("MAX_WARNINGS", 3))
    
except (ValueError, TypeError) as e:
    logging.error(f"❌ Environment variables configuration error: {e}")
    exit()

# --- Initialize Database ---
db = JsonDatabase("database.json")
logging.info("✅ JSON Database initialized successfully!")

# --- Pyrogram Client ---
app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Chat Join Request Handler ---

@app.on_chat_join_request()
async def join_request_handler(client: Client, chat_join_request):
    user_id = chat_join_request.from_user.id
    chat_id = chat_join_request.chat.id

    db.join_requests.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {
            "user_id": user_id,
            "chat_id": chat_id,
            "created_at": datetime.now(timezone.utc)
        }},
        upsert=True
    )
    logging.info(f"Recorded join request for user {user_id} in chat {chat_id}")

# --- Helper Functions ---

def generate_random_string(length=8):
    """Generates a random string."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def get_unique_id(collection):
    """Generates a unique ID for collections."""
    for _ in range(10):
        random_id = generate_random_string()
        if collection.find_one({"_id": random_id}) is None:
            return random_id
        await asyncio.sleep(0.01)
    raise Exception("Failed to generate unique ID after multiple attempts.")

async def get_user_full_name(user):
    """Safely gets the user's full name."""
    if user:
        full_name = user.first_name if user.first_name else ""
        if user.last_name:
            full_name += f" {user.last_name}"
        return full_name.strip() if full_name else f"User_{user.id}"
    return "Unknown User"

async def get_required_channels_data(client: Client, extra_channels: list = None) -> list:
    """
    Collects channel info dicts for all required force join channels (from env, DB, and extra).
    Each dict has: {'chat_id', 'title', 'username', 'invite_link', 'identifier'}
    """
    channel_list = []
    seen_identifiers = set()

    # 1. Dynamic force channels from database
    db_fs_channels = list(db.force_channels.find({}))
    for ch_doc in db_fs_channels:
        ident = str(ch_doc.get("chat_id") or ch_doc.get("_id"))
        if ident in seen_identifiers:
            continue
        seen_identifiers.add(ident)
        channel_list.append({
            "chat_id": ch_doc.get("chat_id"),
            "title": ch_doc.get("title") or "Required Channel",
            "username": ch_doc.get("username"),
            "invite_link": ch_doc.get("invite_link"),
            "identifier": ident
        })

    # 2. Environment variable FORCE_CHANNELS
    for env_ch in FORCE_CHANNELS:
        clean_ch = env_ch.strip().replace("@", "")
        if clean_ch and clean_ch.lower() not in seen_identifiers:
            seen_identifiers.add(clean_ch.lower())
            channel_list.append({
                "chat_id": f"@{clean_ch}",
                "title": f"@{clean_ch}",
                "username": clean_ch,
                "invite_link": f"https://t.me/{clean_ch}",
                "identifier": clean_ch.lower()
            })

    # 3. Extra channels (e.g. per-file custom force_channel)
    if extra_channels:
        for extra in extra_channels:
            if not extra:
                continue
            clean_extra = extra.strip().replace("@", "")
            if clean_extra and clean_extra.lower() not in seen_identifiers:
                seen_identifiers.add(clean_extra.lower())
                channel_list.append({
                    "chat_id": f"@{clean_extra}",
                    "title": f"@{clean_extra}",
                    "username": clean_extra,
                    "invite_link": f"https://t.me/{clean_extra}",
                    "identifier": clean_extra.lower()
                })

    return channel_list

async def get_missing_channels_for_user(client: Client, user_id: int, channels_data: list) -> list:
    """
    Checks if user is member OR has requested to join for each channel.
    Returns list of channel dicts where user has NOT joined or requested.
    """
    missing = []
    for ch in channels_data:
        target = ch.get("chat_id") or (f"@{ch['username']}" if ch.get("username") else None)
        if not target:
            continue

        resolved_chat_id = None
        is_satisfied = False

        # Try to resolve chat object
        try:
            chat_obj = await client.get_chat(target)
            resolved_chat_id = chat_obj.id
            if not ch.get("invite_link") and chat_obj.invite_link:
                ch["invite_link"] = chat_obj.invite_link
            if chat_obj.title and ch["title"].startswith("@"):
                ch["title"] = chat_obj.title
        except Exception as e:
            logging.warning(f"Could not fetch chat object for {target}: {e}")

        # Check membership using Pyrogram
        try:
            member = await client.get_chat_member(target, user_id)
            if member.status in ["owner", "administrator", "member", "restricted"]:
                is_satisfied = True
        except UserNotParticipant:
            pass
        except Exception as e:
            logging.error(f"Error checking chat member status for {user_id} in {target}: {e}")

        # Check if user submitted a join request
        if not is_satisfied:
            filter_query = {"user_id": user_id}
            if resolved_chat_id:
                req = db.join_requests.find_one({"user_id": user_id, "chat_id": resolved_chat_id})
            else:
                req = db.join_requests.find_one({"user_id": user_id, "chat_id": target})

            if req:
                is_satisfied = True

        if not is_satisfied:
            missing.append(ch)

    return missing

async def is_user_member_all_channels(client: Client, user_id: int, channels: list) -> list:
    """Legacy helper maintained for backward compatibility."""
    channels_data = await get_required_channels_data(client, extra_channels=channels)
    missing_data = await get_missing_channels_for_user(client, user_id, channels_data)
    return [m.get("username") or str(m.get("chat_id")) for m in missing_data]

async def get_bot_mode(db) -> str:
    """Fetches the current bot operation mode."""
    setting = db.settings.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    db.settings.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

def force_join_check(func):
    """Decorator to check if a user is a member of all required channels or requested to join."""
    async def wrapper(client, message):
        user_id = message.from_user.id
        extra_channels = []
        file_id_str = None

        if isinstance(message, Message) and message.text:
            parsed_url = urllib.parse.urlparse(message.text)
            if parsed_url.query:
                file_id_str = urllib.parse.parse_qs(parsed_url.query).get('start', [None])[0]
        
        if isinstance(message, Message) and message.command and len(message.command) > 1 and message.command[0] in ["start"]:
             file_id_str = message.command[1]

        if file_id_str and file_id_str != 'force':
            file_record = db.files.find_one({"_id": file_id_str})
            multi_file_record = db.multi_files.find_one({"_id": file_id_str})
            
            if file_record and file_record.get('force_channel'):
                extra_channels.append(file_record['force_channel'])
            elif multi_file_record and multi_file_record.get('force_channel'):
                extra_channels.append(multi_file_record['force_channel'])
        
        channels_data = await get_required_channels_data(client, extra_channels=extra_channels)
        missing_channels = await get_missing_channels_for_user(client, user_id, channels_data)
        
        if missing_channels:
            join_buttons = []
            for ch in missing_channels:
                btn_text = to_small_caps(f"🔗 Join / Request {ch['title']}")
                url = ch.get("invite_link") or (f"https://t.me/{ch['username']}" if ch.get("username") else None)
                if url:
                    join_buttons.append([InlineKeyboardButton(btn_text, url=url)])

            callback_data = f"check_join_{file_id_str}" if file_id_str else "check_join_force"
            join_buttons.append([InlineKeyboardButton(to_small_caps("🔄 Try Again"), callback_data=callback_data)])
            
            await message.reply(
                to_small_caps("🛑 ACCESS DENIED 🛑\n\nTo access this file/feature, please join or request to join the required channels below:"),
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return
        
        db.users.update_one(
             {"_id": user_id},
             {"$set": {"last_activity": datetime.now(timezone.utc)}},
             upsert=True
        )
        
        return await func(client, message)
    return wrapper

async def delete_files_after_delay(client: Client, chat_id: int, message_ids: list):
    """Deletes a list of messages after a 60-minute delay."""
    await asyncio.sleep(3600)
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
        logging.info(f"Successfully auto-deleted messages {message_ids} for user {chat_id}.")
    except Exception as e:
        if "MESSAGE_NOT_FOUND" not in str(e):
            logging.error(f"Failed to auto-delete messages {message_ids} for user {chat_id}: {e}")

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
@force_join_check
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_name = await get_user_full_name(message.from_user)
    
    db.users.update_one(
        {"_id": user_id}, 
        {"$set": {"name": user_name, "last_activity": datetime.now(timezone.utc)}},
        upsert=True
    )

    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        file_record = db.files.find_one({"_id": file_id_str})
        multi_file_record = db.multi_files.find_one({"_id": file_id_str})
        
        if file_record:
            try:
                sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                await message.reply(to_small_caps("🎉 File Unlocked! It will be auto-deleted in 60 minutes to save space."), quote=True)
                asyncio.create_task(delete_files_after_delay(client, user_id, [sent_message.id]))
            except Exception as e:
                await message.reply(to_small_caps(f"❌ An error occurred while sending the file.\nError: {e}"))
            return

        if multi_file_record:
            sent_message_ids = []
            file_title = multi_file_record.get('file_name', f"Bundle of {len(multi_file_record['message_ids'])} Files")
            
            await message.reply(to_small_caps(f"📦 Bundle Unlocked! Sending {file_title} now. This will be auto-deleted in 60 minutes."), quote=True)

            for msg_id in multi_file_record['message_ids']:
                try:
                    sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_message_ids.append(sent_message.id)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            
            asyncio.create_task(delete_files_after_delay(client, user_id, sent_message_ids))
            return
        
        await message.reply(to_small_caps("🤔 File/Bundle Not Found! The link might be wrong, expired, or deleted by the owner."))
    else:
        buttons = [
            [InlineKeyboardButton(to_small_caps("📚 About This Bot"), callback_data="about"),
             InlineKeyboardButton(to_small_caps("💡 How to Use?"), callback_data="help")],
            [InlineKeyboardButton(to_small_caps("⚙️ My Files & Settings"), callback_data="my_files_menu")]
        ]
        
        start_photo_id_doc = db.settings.find_one({"_id": "start_photo"})
        start_photo_id = start_photo_id_doc.get("file_id") if start_photo_id_doc and start_photo_id_doc.get("file_id") else None

        caption_text = to_small_caps(
            f"Hello, {message.from_user.first_name}! I'm FileLinker Bot! 🤖\n\n"
            "I convert your files into permanent, shareable links. "
            "Just send me a file or start a bundle with /multi_link ! ✨"
        )
        
        try:
            if start_photo_id:
                await message.reply_photo(
                    photo=start_photo_id,
                    caption=caption_text,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                await message.reply(
                    caption_text,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
        except Exception:
             await message.reply(
                 caption_text,
                 reply_markup=InlineKeyboardMarkup(buttons)
             )


@app.on_message(filters.command("help") & filters.private)
async def help_handler_private(client: Client, message: Message):
    text = to_small_caps(
        "💡 FileLinker Bot Usage Guide\n\n"
        "1. Single File Link:\n"
        "   - Send me any file (document, video, photo, audio).\n"
        "   - Custom Force Join: Use /create_link @channel_username [Title] then send the file.\n\n"
        "2. Multi-File Bundle Link:\n"
        "   - Start the bundle: /multi_link [Title for bundle]\n"
        "   - Forward all your files to me.\n"
        "   - Finish: Send /done .\n"
        "   - Custom Force Join: Use /multi_link @channel_username [Title]\n\n"
        "3. Set Thumbnail:\n"
        "   - Reply to a photo with: /set_thumbnail\n"
        "   - The next file or bundle will use that photo as its thumbnail.\n\n"
        "4. Management:\n"
        "   - My Files: /myfiles (View your last 10 uploads).\n"
        "   - Delete: /delete <file_id> (Permanently delete your file/bundle).\n\n"
        "5. Inline Search (Everywhere):\n"
        "   - In any chat, type: @bot_username <file_name> to search and share links instantly!"
    )
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("create_link") & filters.private)
@force_join_check
async def create_link_handler(client: Client, message: Message):
    if len(message.command) == 1 or (len(message.command) > 1 and not message.command[1].startswith('@')):
        file_name = " ".join(message.command[1:]) if len(message.command) > 1 else None
        
        user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
        thumbnail_id = user_state.get("thumbnail_id") if user_state else None
            
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$set": {"state": "single_link", "force_channel": None, "file_name": file_name, "thumbnail_id": thumbnail_id}},
            upsert=True
        )
        await message.reply(to_small_caps("Okay! Now send me a single file to generate a link."))
        return
        
    channel_index = 1
    if not message.command[channel_index].startswith('@'):
         return await create_link_handler(client, message)
         
    force_channel = message.command[channel_index].replace('@', '').strip()
    file_name = " ".join(message.command[channel_index+1:]) if len(message.command) > channel_index+1 else None
    
    try:
        chat = await client.get_chat(force_channel)
        if chat.type != 'channel':
            await message.reply(to_small_caps("❌ That is not a valid public channel username. Please provide a public channel username."))
            return
        
        await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
        
        user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
        thumbnail_id = user_state.get("thumbnail_id") if user_state else None
        
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$set": {"state": "single_link", "force_channel": force_channel, "file_name": file_name, "thumbnail_id": thumbnail_id}},
            upsert=True
        )
        
        await message.reply(to_small_caps(f"✅ Force join channel set to @{force_channel}. Now send me a file to get its link."))
        
    except ChatAdminRequired:
         await message.reply(to_small_caps("❌ I'm not an admin in that channel. Please check my permissions."))
    except Exception as e:
        await message.reply(to_small_caps(f"❌ I could not find that channel or I'm not a member there. Please make sure the channel is public and I have access.\nError: {e}"))

@app.on_message(filters.command("set_thumbnail") & filters.private)
@force_join_check
async def set_thumbnail_handler(client: Client, message: Message):
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.reply(to_small_caps("🖼️ Set Thumbnail\n\nPlease reply to the photo you wish to use as a thumbnail for your next upload/bundle, and then send /set_thumbnail ."))
        return
        
    thumbnail_id = message.reply_to_message.photo.file_id
    
    db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp_link"},
        {"$set": {"thumbnail_id": thumbnail_id, "state": "single_link"}},
        upsert=True
    )
    
    await message.reply(to_small_caps("✅ Thumbnail Set!\n\nThe next file you upload (or the next /multi_link bundle) will use this thumbnail. Send /cancel_thumbnail to remove it."))

@app.on_message(filters.command("cancel_thumbnail") & filters.private)
@force_join_check
async def cancel_thumbnail_handler(client: Client, message: Message):
    result = db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp_link"},
        {"$unset": {"thumbnail_id": ""}}
    )
    
    if result.modified_count > 0:
         await message.reply(to_small_caps("✅ Custom Thumbnail Cancelled! Future uploads will use default thumbnails."))
    else:
         await message.reply(to_small_caps("❌ No custom thumbnail was set to be cancelled."))

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
@force_join_check
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode(db)
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply(to_small_caps("😔 Bot is in Private Mode! Only Admins can upload files right now."))
        return

    user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
    thumbnail_id = user_state.get("thumbnail_id") if user_state else None
    
    if user_state and user_state.get("state") == "multi_link":
        if (message.video and message.video.file_size > (2 * 1024 * 1024 * 1024)) or \
           (message.document and message.document.file_size > (2 * 1024 * 1024 * 1024)):
             await message.reply(to_small_caps("⚠️ File is too large to be added to the bundle. Max limit is 2GB."), quote=True)
             return
             
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$push": {"message_ids": message.id}}
        )
        
        new_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
        new_count = len(new_state.get("message_ids", []))
        
        await message.reply(to_small_caps(f"📦 File #{new_count} added to the bundle. Send more or use /done to finish."), quote=True)
        return
    
    status_msg = await message.reply(to_small_caps("⏳ Processing File... Please wait while I create your link. 🔗"), quote=True)
    
    try:
        original_message = message
        thumb_kwargs = {}
        if thumbnail_id and (original_message.document or original_message.video or original_message.audio):
             thumb_kwargs['thumb'] = thumbnail_id
        
        forwarded_message = await client.copy_message( 
            chat_id=LOG_CHANNEL, 
            from_chat_id=message.chat.id, 
            message_id=message.id,
            caption=original_message.caption,
            reply_markup=original_message.reply_markup,
            **thumb_kwargs
        ) 
        
        file_id_str = await get_unique_id(db.files) 
        
        file_name = "Untitled"
        file_type = "unknown"
        if message.document:
            file_name = message.document.file_name or "Document"
            file_type = "document"
        elif message.video:
            file_name = message.video.file_name or "Video"
            file_type = "video"
        elif message.photo:
            file_name = message.caption or f"Photo_{forwarded_message.id}"
            file_type = "photo"
        elif message.audio:
            file_name = message.audio.title or "Audio"
            file_type = "audio"
            
        if user_state and user_state.get("file_name"):
            file_name = user_state["file_name"]
            
        force_channel = user_state.get("force_channel") if user_state and user_state.get("state") == "single_link" else None
        
        db.files.insert_one({
            '_id': file_id_str,
            'message_id': forwarded_message.id,
            'user_id': message.from_user.id,
            'file_name': file_name,
            'file_type': file_type,
            'force_channel': force_channel,
            'created_at': datetime.now(timezone.utc)
        })
        
        db.settings.delete_one({"_id": message.from_user.id, "type": "temp_link"})
        
        bot_username = (await client.get_me()).username
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        share_text = f"File: {file_name}\nLink: {share_link}"
        share_button = InlineKeyboardButton(to_small_caps("📤 Share Link"), url=f"https://t.me/share/url?url={urllib.parse.quote(share_text)}")
        
        reply_text = to_small_caps(
            f"🎉 Link Generated Successfully! 🎉\n\n"
            f"🗂️ File Name: `{file_name}`\n"
            f"🔗 Permanent Link: `{share_link}`\n\n"
            f"Note: Share this link anywhere, and the file will be delivered directly from the bot!"
        )
        
        if force_channel:
            reply_text += to_small_caps(f"\n\n🔒 Access Condition: User must join @{force_channel} .")
        
        if thumbnail_id:
            reply_text += to_small_caps("\n\n🖼️ Custom thumbnail applied!")
            
        await status_msg.edit_text(
            reply_text,
            reply_markup=InlineKeyboardMarkup([[share_button]]),
            disable_web_page_preview=True
        )
        
        log_text = to_small_caps(
            f"🆕 New Single File Link\n"
            f"• User: {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
            f"• File: `{file_name}`"
        )
        if thumbnail_id:
             log_text += to_small_caps(" (🖼️ Custom Thumb)")
        log_text += f"\n• Link: `t.me/{bot_username}?start={file_id_str}`"
        
        await client.send_message(LOG_CHANNEL, log_text)

    except Exception as e:
        logging.error(f"Single file handling error: {e}", exc_info=True)
        await status_msg.edit_text(to_small_caps(f"❌ Error!\n\nSomething went wrong while processing the file. Please try again.\nDetails: {e}"))


@app.on_message(filters.command("multi_link") & filters.private)
@force_join_check
async def multi_link_handler(client: Client, message: Message):
    command_parts = message.command[1:]
    force_channel = None
    file_name = None

    if command_parts:
        if command_parts[0].startswith('@'):
            force_channel = command_parts[0].replace('@', '').strip()
            file_name = " ".join(command_parts[1:])
        else:
            file_name = " ".join(command_parts)
    
    user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
    thumbnail_id = user_state.get("thumbnail_id") if user_state else None
    
    if force_channel:
        try:
            chat = await client.get_chat(force_channel)
            if chat.type != 'channel':
                await message.reply(to_small_caps("❌ That is not a valid public channel username."))
                return
            await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
            
            db.settings.update_one(
                {"_id": message.from_user.id, "type": "temp_link"},
                {"$set": {"state": "multi_link", "message_ids": [], "force_channel": force_channel, "file_name": file_name, "thumbnail_id": thumbnail_id}},
                upsert=True
            )
            await message.reply(to_small_caps(f"✅ Force join channel set to @{force_channel}. Now, forward files for the bundle. Send /done to finish."))
            return
            
        except ChatAdminRequired:
            await message.reply(to_small_caps("❌ I'm not an admin in that channel. Please check my permissions."))
            return
        except Exception as e:
            await message.reply(to_small_caps(f"❌ I could not find that channel or I'm not a member there. Please check the username.\nError: {e}"))
            return

    db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp_link"},
        {"$set": {"state": "multi_link", "message_ids": [], "force_channel": None, "file_name": file_name, "thumbnail_id": thumbnail_id}},
        upsert=True
    )
    
    reply_text = to_small_caps(
        "📦 Multi-File Bundle Mode Activated!\n\n"
        "Now, forward me all the files you want to bundle together. "
        "When you're finished, send the command /done ."
    )
    
    if thumbnail_id:
         reply_text += to_small_caps("\n\n🖼️ Note: A custom thumbnail is currently set and will be applied to the files in this bundle.")
    
    await message.reply(reply_text)

@app.on_message(filters.command("done") & filters.private)
@force_join_check
async def done_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_state = db.settings.find_one({"_id": user_id, "type": "temp_link"})
    
    if user_state and user_state.get("state") == "multi_link":
        message_ids = user_state.get("message_ids", [])
        thumbnail_id = user_state.get("thumbnail_id") 
        
        if not message_ids:
            await message.reply(to_small_caps("❌ You haven't added any files. Please forward them first or use /multi_link again."))
            return
            
        status_msg = await message.reply(to_small_caps(f"⏳ Finishing Bundle! Processing {len(message_ids)} files..."))
        
        try:
            forwarded_msg_ids = []
            for msg_id in message_ids:
                try:
                    original_message = await client.get_messages(user_id, msg_id) 
                    
                    thumb_kwargs = {}
                    if thumbnail_id and (original_message.document or original_message.video or original_message.audio):
                         thumb_kwargs['thumb'] = thumbnail_id
                    
                    forwarded_msg = await client.copy_message(
                        chat_id=LOG_CHANNEL, 
                        from_chat_id=user_id, 
                        message_id=msg_id,
                        caption=original_message.caption,
                        reply_markup=original_message.reply_markup,
                        **thumb_kwargs
                    ) 
                    forwarded_msg_ids.append(forwarded_msg.id)
                    await asyncio.sleep(0.1) 
                except Exception as e:
                    logging.error(f"Error copying message {msg_id} for bundle: {e}")
            
            multi_file_id = await get_unique_id(db.multi_files) 
            force_channel = user_state.get("force_channel")
            file_name = user_state.get("file_name") or f"Bundle of {len(forwarded_msg_ids)} Files"
            
            db.multi_files.insert_one({
                '_id': multi_file_id, 
                'message_ids': forwarded_msg_ids,
                'user_id': user_id,
                'file_name': file_name,
                'force_channel': force_channel,
                'created_at': datetime.now(timezone.utc)
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={multi_file_id}"
            
            db.settings.delete_one({"_id": user_id, "type": "temp_link"})
            
            share_text = f"Bundle: {file_name}\nLink: {share_link}"
            share_button = InlineKeyboardButton(to_small_caps("📤 Share Bundle Link"), url=f"https://t.me/share/url?url={urllib.parse.quote(share_text)}")
            
            reply_text = to_small_caps(
                f"🎉 Multi-File Bundle Link Generated! 🎉\n\n"
                f"📦 Bundle Name: `{file_name}`\n"
                f"#️⃣ Total Files: **{len(forwarded_msg_ids)}**\n"
                f"🔗 Permanent Link: `{share_link}`"
            )
            
            if force_channel:
                 reply_text += to_small_caps(f"\n\n🔒 Access Condition: User must join @{force_channel} .")
            
            if thumbnail_id:
                reply_text += to_small_caps("\n\n🖼️ Custom thumbnail applied to compatible files!")
                
            await status_msg.edit_text(
                reply_text,
                reply_markup=InlineKeyboardMarkup([[share_button]]),
                disable_web_page_preview=True
            )
            
            log_text = to_small_caps(
                f"📦 New Multi-File Link\n"
                f"• User: {await get_user_full_name(message.from_user)} (`{user_id}`)\n"
                f"• Bundle: `{file_name}` ({len(forwarded_msg_ids)} files)"
            )
            if thumbnail_id:
                 log_text += to_small_caps(" (🖼️ Custom Thumb)")
            log_text += f"\n• Link: `t.me/{bot_username}?start={multi_file_id}`"
            
            await client.send_message(LOG_CHANNEL, log_text)

        except Exception as e:
            logging.error(f"Multi-file link creation error: {e}", exc_info=True)
            await status_msg.edit_text(to_small_caps(f"❌ Error!\n\nSomething went wrong while creating the bundle. Please try again.\nDetails: {e}"))
    else:
        await message.reply(to_small_caps("🤔 You are not in multi-link mode. Send /multi_link [Optional Title] to start a new bundle."))


@app.on_message(filters.command("myfiles") & filters.private)
async def my_files_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    user_single_files = list(db.files.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    user_multi_files = list(db.multi_files.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    
    if not user_single_files and not user_multi_files:
        await message.reply(to_small_caps("😔 You haven't uploaded any files or created any bundles yet. Start with sending a file or /multi_link ."))
        return

    text = to_small_caps("📂 Your Recent Uploads & Bundles:\n\n")
    bot_username = (await client.get_me()).username
    
    if user_single_files:
        text += to_small_caps("--- Single Files (Last 5) ---\n")
        for i, file_record in enumerate(user_single_files):
            file_name = file_record.get('file_name', 'Unnamed File')
            file_id_str = file_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `🔗` [{file_name}]({share_link})\n"
        text += "\n"
        
    if user_multi_files:
        text += to_small_caps("--- Multi-File Bundles (Last 5) ---\n")
        for i, bundle_record in enumerate(user_multi_files):
            file_name = bundle_record.get('file_name', f"Bundle of {len(bundle_record.get('message_ids', []))} Files")
            file_id_str = bundle_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `📦` [{file_name}]({share_link})\n"
        text += "\n"

    text += to_small_caps("To delete a file, use: /delete <file_id>")
    
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("delete") & filters.private)
async def delete_file_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply(to_small_caps("Please provide the file or bundle ID to delete. Example: /delete abcdefgh"))
        return

    file_id_str = message.command[1].split('?start=')[-1]
    user_id = message.from_user.id
    
    file_record = db.files.find_one({"_id": file_id_str, "user_id": user_id})
    multi_file_record = db.multi_files.find_one({"_id": file_id_str, "user_id": user_id})
    
    is_single_file = bool(file_record)
    record_to_delete = file_record or multi_file_record

    if not record_to_delete:
        await message.reply(to_small_caps("🤔 File or bundle not found, or you don't have permission to delete it."))
        return
        
    file_name = record_to_delete.get('file_name', 'Unnamed Item')

    delete_button = InlineKeyboardButton(to_small_caps("🗑️ Confirm Delete"), callback_data=f"confirm_delete_{file_id_str}_{'single' if is_single_file else 'multi'}")
    cancel_button = InlineKeyboardButton(to_small_caps("↩️ Cancel"), callback_data="cancel_delete")
    keyboard = InlineKeyboardMarkup([[delete_button, cancel_button]])

    item_type = "File" if is_single_file else "Bundle"
    
    await message.reply(
        to_small_caps(
            f"⚠️ Confirm Deletion\n\n"
            f"Are you sure you want to permanently delete this {item_type}:\n`{file_name}`?"
        ),
        reply_markup=keyboard,
        quote=True
    )

# --- Admin Handlers ---

@app.on_message(filters.command("admin") & filters.private & filters.user(ADMINS))
async def admin_panel_handler(client: Client, message: Message):
    current_mode = await get_bot_mode(db)
    
    buttons = [
        [InlineKeyboardButton(to_small_caps("📊 Bot Stats"), callback_data="admin_stats"),
         InlineKeyboardButton(to_small_caps(f"⚙️ Mode: {current_mode.upper()}"), callback_data="admin_settings")],
        [InlineKeyboardButton(to_small_caps("📣 Broadcast Message"), callback_data="admin_broadcast_prompt")]
    ]
    await message.reply(
        to_small_caps(
            "👑 Admin Panel Access Granted! 🛡️\n\n"
            "Welcome back! Manage your bot's operation and check statistics below."
        ),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats_handler(client: Client, message: Message):
    user_count = db.users.count_documents({})
    single_files_count = db.files.count_documents({})
    multi_files_count = db.multi_files.count_documents({})
    total_files_count = single_files_count + multi_files_count

    today_start_dt = datetime.now(timezone.utc) - timedelta(days=1)
    
    today_new_users = db.users.count_documents({"last_activity": {"$gte": today_start_dt}})
    today_single_files = db.files.count_documents({"created_at": {"$gte": today_start_dt}})
    today_multi_files = db.multi_files.count_documents({"created_at": {"$gte": today_start_dt}})
    
    file_types_cursor = db.files.aggregate([{"$group": {"_id": "$file_type", "count": {"$sum": 1}}}])
    file_types_text = "\n".join([f"  • {ft['_id'].capitalize()}: **{ft['count']}**" for ft in file_types_cursor if ft['_id']])
    if not file_types_text:
        file_types_text = "  • No files recorded."
    
    await message.reply(
        to_small_caps(
            f"📊 BOT STATISTICS\n\n"
            f"--- User & Usage ---\n"
            f"👥 Total Users: `{user_count}`\n"
            f"🗓️ Active (Last 24h): `{today_new_users}`\n\n"
            f"--- Files ---\n"
            f"📁 Total Items: `{total_files_count}`\n"
            f"📄 Single Files: `{single_files_count}`\n"
            f"📦 Multi-Bundles: `{multi_files_count}`\n"
            f"📈 Uploads (Last 24h): `{today_single_files + today_multi_files}`\n\n"
            f"--- File Breakdown ---\n"
        ) + file_types_text
    )

@app.on_message(filters.command(["setfs", "set_fs"]) & filters.private & filters.user(ADMINS))
async def set_fs_handler(client: Client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        await message.reply(
            to_small_caps(
                "❌ Usage: /setfs <channel_id or @username or invite_link>\n"
                "Or reply to a forwarded message from the channel with /setfs ."
            )
        )
        return

    target_chat = None
    if message.reply_to_message and message.reply_to_message.forward_from_chat:
        target_chat = message.reply_to_message.forward_from_chat.id
    elif len(message.command) >= 2:
        target_chat = message.command[1].strip()

    if not target_chat:
        await message.reply(to_small_caps("❌ Invalid channel specified."))
        return

    if isinstance(target_chat, str):
        if target_chat.startswith("https://t.me/"):
            target_chat = target_chat.replace("https://t.me/", "")
            if not target_chat.startswith("+") and not target_chat.startswith("joinchat/") and "/" in target_chat:
                target_chat = target_chat.split("/")[0]
        if target_chat.startswith("@"):
            target_chat = target_chat[1:]
        if target_chat.lstrip("-").isdigit():
            target_chat = int(target_chat)

    try:
        chat = await client.get_chat(target_chat)
    except Exception as e:
        await message.reply(to_small_caps(f"❌ Failed to get chat details: {e}\nMake sure the bot is an admin in the channel."))
        return

    bot_me = await client.get_me()
    try:
        bot_member = await client.get_chat_member(chat.id, bot_me.id)
        if bot_member.status not in ["administrator", "owner"]:
            await message.reply(to_small_caps("❌ I am not an admin in that channel. Please promote me to admin first."))
            return
    except Exception as e:
        await message.reply(to_small_caps(f"❌ Could not check admin permissions in channel: {e}"))
        return

    invite_link = chat.invite_link
    if not invite_link:
        try:
            invite_link = await client.export_chat_invite_link(chat.id)
        except Exception:
            try:
                created_link = await client.create_chat_invite_link(chat.id, creates_join_request=True)
                invite_link = created_link.invite_link
            except Exception:
                if chat.username:
                    invite_link = f"https://t.me/{chat.username}"

    db.force_channels.update_one(
        {"_id": str(chat.id)},
        {"$set": {
            "chat_id": chat.id,
            "title": chat.title,
            "username": chat.username,
            "invite_link": invite_link,
            "created_at": datetime.now(timezone.utc)
        }},
        upsert=True
    )

    await message.reply(
        to_small_caps(
            f"✅ Force Sub Channel Set Successfully!\n\n"
            f"• Title: {chat.title}\n"
            f"• ID: `{chat.id}`\n"
            f"• Username: @{chat.username if chat.username else 'N/A'}\n"
            f"• Link: {invite_link or 'N/A'}"
        ),
        disable_web_page_preview=True
    )

@app.on_message(filters.command(["remfs", "del_fs", "delfs"]) & filters.private & filters.user(ADMINS))
async def rem_fs_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply(to_small_caps("❌ Usage: /remfs <channel_id or @username>"))
        return

    target = message.command[1].strip()
    if target.startswith("@"):
        target = target[1:]

    channel_doc = None
    if target.lstrip("-").isdigit():
        channel_doc = db.force_channels.find_one({"_id": target}) or db.force_channels.find_one({"chat_id": int(target)})

    if not channel_doc:
        channel_doc = db.force_channels.find_one({"username": target})

    if not channel_doc:
        for doc in db.force_channels.find({}):
            if str(doc.get("chat_id")) == target or str(doc.get("_id")) == target or (doc.get("username") and doc.get("username").lower() == target.lower()):
                channel_doc = doc
                break

    if not channel_doc:
        await message.reply(to_small_caps("❌ Force sub channel not found in database."))
        return

    db.force_channels.delete_one({"_id": channel_doc["_id"]})
    await message.reply(to_small_caps(f"✅ Removed Force Sub Channel: {channel_doc.get('title', 'Channel')} (`{channel_doc.get('_id')}`)"))

@app.on_message(filters.command(["getfs", "fs_channels", "listfs"]) & filters.private & filters.user(ADMINS))
async def get_fs_handler(client: Client, message: Message):
    fs_list = list(db.force_channels.find({}))

    text = to_small_caps("📋 Force Sub Channels List:\n\n")

    if fs_list:
        text += to_small_caps("--- Dynamic Force Sub Channels ---\n")
        for i, ch in enumerate(fs_list, 1):
            title = ch.get("title", "Channel")
            chat_id = ch.get("chat_id") or ch.get("_id")
            username = f"@{ch.get('username')}" if ch.get("username") else "Private"
            link = ch.get("invite_link", "No link")
            text += f"**{i}.** [{title}]({link}) (`{chat_id}`) | {username}\n"
        text += "\n"
    else:
        text += to_small_caps("No dynamic Force Sub channels configured via /setfs .\n\n")

    if FORCE_CHANNELS:
        text += to_small_caps("--- Environment FORCE_CHANNELS ---\n")
        for ch in FORCE_CHANNELS:
            text += f"• @{ch}\n"

    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_handler_reply_enhanced(client: Client, message: Message):
    if not message.reply_to_message and len(message.command) < 2:
        await message.reply(
            to_small_caps(
                "📣 Broadcast Mode\n\n"
                "Please reply to the message/media you want to broadcast and use /broadcast .\n"
                "Or, send the text immediately after the command: /broadcast Hello everyone!\n\n"
                "Note: Formatting and media are supported."
            )
        )
        return

    broadcast_message = message.reply_to_message or message
    
    if broadcast_message == message and len(message.command) > 1:
        text_to_send = message.text.split(" ", 1)[1]
    elif message.reply_to_message:
        text_to_send = None
    else:
        await message.reply(to_small_caps("Error: Could not determine broadcast content."))
        return
        
    users = db.users.find({})
    user_ids = [user['_id'] for user in users]
    
    success_count = 0
    failed_count = 0
    
    status_msg = await message.reply(to_small_caps(f"⏳ Starting broadcast to {len(user_ids)} users..."))
    
    async def send_message_task(chat_id, content_message, text_override):
        nonlocal success_count, failed_count
        try:
            if text_override:
                await client.send_message(chat_id=chat_id, text=text_override, disable_web_page_preview=True)
            elif content_message:
                await content_message.copy(chat_id)
            success_count += 1
        except Exception:
            failed_count += 1
            db.users.delete_one({"_id": chat_id})
        await asyncio.sleep(0.1)

    tasks = []
    for uid in user_ids:
        if uid != message.from_user.id:
             tasks.append(send_message_task(uid, message.reply_to_message, text_to_send))
             
    await asyncio.gather(*tasks)
    
    await status_msg.edit_text(
        to_small_caps(
            f"✅ Broadcast Complete!\n\n"
            f"Success: `{success_count}`\n"
            f"Failed (Blocked/Left/Cleaned): `{failed_count}`"
        )
    )

# --- Callback Query Handlers ---

def get_start_content(user):
    first_name = user.first_name if user and user.first_name else "User"
    buttons = [
        [InlineKeyboardButton(to_small_caps("📚 About This Bot"), callback_data="about"),
         InlineKeyboardButton(to_small_caps("💡 How to Use?"), callback_data="help")],
        [InlineKeyboardButton(to_small_caps("⚙️ My Files & Settings"), callback_data="my_files_menu")]
    ]
    caption_text = to_small_caps(
        f"Hello, {first_name}! I'm FileLinker Bot! 🤖\n\n"
        "I convert your files into permanent, shareable links. "
        "Just send me a file or start a bundle with /multi_link ! ✨"
    )
    return caption_text, InlineKeyboardMarkup(buttons)

async def get_my_files_content(user_id, bot_username):
    user_single_files = list(db.files.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    user_multi_files = list(db.multi_files.find({"user_id": user_id}).sort("created_at", -1).limit(5))

    if not user_single_files and not user_multi_files:
        return to_small_caps("😔 You haven't uploaded any files or created any bundles yet. Start with sending a file or /multi_link .")

    text = to_small_caps("📂 Your Recent Uploads & Bundles:\n\n")

    if user_single_files:
        text += to_small_caps("--- Single Files (Last 5) ---\n")
        for i, file_record in enumerate(user_single_files):
            file_name = file_record.get('file_name', 'Unnamed File')
            file_id_str = file_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `🔗` [{file_name}]({share_link})\n"
        text += "\n"

    if user_multi_files:
        text += to_small_caps("--- Multi-File Bundles (Last 5) ---\n")
        for i, bundle_record in enumerate(user_multi_files):
            file_name = bundle_record.get('file_name', f"Bundle of {len(bundle_record.get('message_ids', []))} Files")
            file_id_str = bundle_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `📦` [{file_name}]({share_link})\n"
        text += "\n"

    text += to_small_caps("To delete a file, use: /delete <file_id>")
    return text

def get_stats_content():
    user_count = db.users.count_documents({})
    single_files_count = db.files.count_documents({})
    multi_files_count = db.multi_files.count_documents({})
    total_files_count = single_files_count + multi_files_count

    today_start_dt = datetime.now(timezone.utc) - timedelta(days=1)

    today_new_users = db.users.count_documents({"last_activity": {"$gte": today_start_dt}})
    today_single_files = db.files.count_documents({"created_at": {"$gte": today_start_dt}})
    today_multi_files = db.multi_files.count_documents({"created_at": {"$gte": today_start_dt}})

    file_types_cursor = db.files.aggregate([{"$group": {"_id": "$file_type", "count": {"$sum": 1}}}])
    file_types_text = "\n".join([f"  • {ft['_id'].capitalize()}: **{ft['count']}**" for ft in file_types_cursor if ft['_id']])
    if not file_types_text:
        file_types_text = "  • No files recorded."

    return to_small_caps(
        f"📊 BOT STATISTICS\n\n"
        f"--- User & Usage ---\n"
        f"👥 Total Users: `{user_count}`\n"
        f"🗓️ Active (Last 24h): `{today_new_users}`\n\n"
        f"--- Files ---\n"
        f"📁 Total Items: `{total_files_count}`\n"
        f"📄 Single Files: `{single_files_count}`\n"
        f"📦 Multi-Bundles: `{multi_files_count}`\n"
        f"📈 Uploads (Last 24h): `{today_single_files + today_multi_files}`\n\n"
        f"--- File Breakdown ---\n"
    ) + file_types_text

@app.on_callback_query(filters.regex("^(about|help|start_menu|my_files_menu|admin_stats|admin_settings|admin_broadcast_prompt|admin|view_my_files|view_force_channels)$"))
async def general_callback_handler(client: Client, callback_query: CallbackQuery):
    query = callback_query.data
    user = callback_query.from_user
    
    if query == "about":
        text = to_small_caps(
            "📚 About FileLinker Bot\n\n"
            "This bot creates permanent, short, and shareable deep-links for your Telegram files. "
            "It's built for efficiency, security, and a great user experience.\n\n"
            "✨ Core Features: File-to-Link, Multi-File Bundling, Optional Force Join, Custom Thumbnails, Inline Search, and Admin Controls.\n\n"
            "Made with ❤️ by [ @narzoxbot ]."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("💡 How to Use?"), callback_data="help"), InlineKeyboardButton(to_small_caps("🔙 Back to Start"), callback_data="start_menu")]])
        
    elif query == "help":
        text = to_small_caps(
            "💡 FileLinker Bot Usage Guide\n\n"
            "1. Single File Link:\n"
            "   - Send me any file (document, video, photo, audio).\n"
            "   - Custom Force Join: Use /create_link @channel_username [Title] then send the file.\n\n"
            "2. Multi-File Bundle Link:\n"
            "   - Start the bundle: /multi_link [Title for bundle]\n"
            "   - Forward all your files to me.\n"
            "   - Finish: Send /done .\n"
            "   - Custom Force Join: Use /multi_link @channel_username [Title]\n\n"
            "3. Set Thumbnail:\n"
            "   - Reply to a photo with: /set_thumbnail\n"
            "   - The next file or bundle will use that photo as its thumbnail.\n\n"
            "4. Management:\n"
            "   - My Files: /myfiles (View your last 10 uploads).\n"
            "   - Delete: /delete <file_id> (Permanently delete your file/bundle).\n\n"
            "5. Inline Search (Everywhere):\n"
            "   - In any chat, type: @bot_username <file_name> to search and share links instantly!"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Start"), callback_data="start_menu")]])

    elif query == "start_menu":
        if user:
            user_name = await get_user_full_name(user)
            db.users.update_one(
                {"_id": user.id},
                {"$set": {"name": user_name, "last_activity": datetime.now(timezone.utc)}},
                upsert=True
            )
        text, keyboard = get_start_content(user)

    elif query == "admin":
        if not user or user.id not in ADMINS:
            await callback_query.answer(to_small_caps("❌ Permission Denied! Only Admins can access this."), show_alert=True)
            return
        current_mode = await get_bot_mode(db)
        buttons = [
            [InlineKeyboardButton(to_small_caps("📊 Bot Stats"), callback_data="admin_stats"),
             InlineKeyboardButton(to_small_caps(f"⚙️ Mode: {current_mode.upper()}"), callback_data="admin_settings")],
            [InlineKeyboardButton(to_small_caps("📣 Broadcast Message"), callback_data="admin_broadcast_prompt")]
        ]
        text = to_small_caps(
            "👑 Admin Panel Access Granted! 🛡️\n\n"
            "Welcome back! Manage your bot's operation and check statistics below."
        )
        keyboard = InlineKeyboardMarkup(buttons)

    elif query == "admin_stats":
        if not user or user.id not in ADMINS:
            await callback_query.answer(to_small_caps("❌ Permission Denied! Only Admins can access this."), show_alert=True)
            return
        text = get_stats_content()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Admin"), callback_data="admin")]])

    elif query == "my_files_menu":
        buttons = [
            [InlineKeyboardButton(to_small_caps("📂 View My Last 10 Files"), callback_data="view_my_files")],
            [InlineKeyboardButton(to_small_caps("🔗 View Force Join Channels"), callback_data="view_force_channels")],
            [InlineKeyboardButton(to_small_caps("🔙 Back to Start"), callback_data="start_menu")]
        ]
        text = to_small_caps("⚙️ My Dashboard\n\nManage your uploaded files and check the current force join channels.")
        keyboard = InlineKeyboardMarkup(buttons)

    elif query == "view_my_files":
        user_id = user.id if user else 0
        bot_username = (await client.get_me()).username
        text = await get_my_files_content(user_id, bot_username)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Menu"), callback_data="my_files_menu")]])

    elif query == "view_force_channels":
        db_fs_channels = list(db.force_channels.find({}))
        if db_fs_channels or FORCE_CHANNELS:
            channels_lines = []
            for ch in db_fs_channels:
                title = ch.get('title', 'Channel')
                link = ch.get('invite_link') or (f"https://t.me/{ch['username']}" if ch.get('username') else "#")
                channels_lines.append(f"• [{title}]({link})")
            for env_ch in FORCE_CHANNELS:
                clean_env_ch = env_ch.strip().replace('@', '')
                if clean_env_ch and not any(d.get('username') and d['username'].lower() == clean_env_ch.lower() for d in db_fs_channels):
                    channels_lines.append(f"• [@{clean_env_ch}](https://t.me/{clean_env_ch})")

            channels_text = "\n".join(channels_lines)
            text = to_small_caps(f"🌐 Global Force Join Channels\n\n{channels_text}\n\nYou must join or request to join these channels to use the bot.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Menu"), callback_data="my_files_menu")]])
        else:
            text = to_small_caps("❌ Global Force Join is NOT active! No channels are required for general use.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Menu"), callback_data="my_files_menu")]])

    elif query == "admin_settings":
        if not user or user.id not in ADMINS:
            await callback_query.answer(to_small_caps("❌ Permission Denied! Only Admins can access this."), show_alert=True)
            return
        current_mode = await get_bot_mode(db)
        public_button = InlineKeyboardButton(to_small_caps("🌍 Public (Anyone)"), callback_data="set_mode_public")
        private_button = InlineKeyboardButton(to_small_caps("🔒 Private (Admins Only)"), callback_data="set_mode_private")
        keyboard = InlineKeyboardMarkup([[public_button], [private_button], [InlineKeyboardButton(to_small_caps("🔙 Back to Admin"), callback_data="admin")]])
        text = to_small_caps(
            f"⚙️ Bot File Upload Mode\n\n"
            f"The current mode is {current_mode.upper()}.\n"
            f"Select a new mode below:"
        )

    elif query == "admin_broadcast_prompt":
        if not user or user.id not in ADMINS:
            await callback_query.answer(to_small_caps("❌ Permission Denied! Only Admins can access this."), show_alert=True)
            return
        text = to_small_caps(
            "📣 Broadcast Message\n\n"
            "Please reply to the message/media you want to broadcast and use /broadcast .\n"
            "Example: /broadcast Check out our new bot features!"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps("🔙 Back to Admin"), callback_data="admin")]])

    try:
        if callback_query.message.photo:
            await callback_query.message.edit_caption(text, reply_markup=keyboard, disable_web_page_preview=True)
        else:
            await callback_query.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        await callback_query.message.reply(text, reply_markup=keyboard, disable_web_page_preview=True)

    await callback_query.answer()
    
@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split("_", 2)
    file_id_str = parts[2] if len(parts) > 2 else None

    extra_channels = []
    if file_id_str and file_id_str != 'force':
        file_record = db.files.find_one({"_id": file_id_str})
        multi_file_record = db.multi_files.find_one({"_id": file_id_str})

        if file_record and file_record.get('force_channel'):
            extra_channels.append(file_record['force_channel'])
        elif multi_file_record and multi_file_record.get('force_channel'):
            extra_channels.append(multi_file_record['force_channel'])
    
    channels_data = await get_required_channels_data(client, extra_channels=extra_channels)
    missing_channels = await get_missing_channels_for_user(client, user_id, channels_data)

    if not missing_channels:
        await callback_query.answer(to_small_caps("Thanks for joining/requesting! Processing now... 🥳"), show_alert=True)
        try:
             await callback_query.message.delete()
        except Exception:
             pass
        
        if file_id_str and file_id_str != 'force':
             fake_message = callback_query.message
             fake_message.from_user = callback_query.from_user
             fake_message.command = ["start", file_id_str]
             await start_handler(client, fake_message)
        else:
             await callback_query.message.reply(to_small_caps("✅ You have joined or requested to join all required channels now! Please try the feature again."))

    else:
        await callback_query.answer(to_small_caps("You have not joined or requested all required channels yet."), show_alert=True)
        join_buttons = []
        for ch in missing_channels:
            btn_text = to_small_caps(f"🔗 Join / Request {ch['title']}")
            url = ch.get("invite_link") or (f"https://t.me/{ch['username']}" if ch.get("username") else None)
            if url:
                join_buttons.append([InlineKeyboardButton(btn_text, url=url)])

        join_buttons.append([InlineKeyboardButton(to_small_caps("✅ I Have Joined / Requested! (Try Again)"), callback_data=callback_query.data)])
        keyboard = InlineKeyboardMarkup(join_buttons)
        
        await callback_query.message.edit_text(
            to_small_caps("❌ ACCESS DENIED\n\nPlease join or send a join request to the remaining channels below:"),
            reply_markup=keyboard
        )

@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer(to_small_caps("❌ Permission Denied! Only Admins can change bot mode."), show_alert=True)
        return
        
    new_mode = callback_query.data.split("_")[2]
    
    db.settings.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": new_mode}},
        upsert=True
    )
    
    await callback_query.answer(to_small_caps(f"Mode successfully set to {new_mode.upper()}!"), show_alert=True)
    
    public_button = InlineKeyboardButton(to_small_caps("🌍 Public (Anyone)"), callback_data="set_mode_public")
    private_button = InlineKeyboardButton(to_small_caps("🔒 Private (Admins Only)"), callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button], [InlineKeyboardButton(to_small_caps("🔙 Back to Admin"), callback_data="admin")]])
    
    await callback_query.message.edit_text(
        to_small_caps(
            f"⚙️ Bot File Upload Mode\n\n"
            f"✅ File upload mode is now {new_mode.upper()}.\n\n"
            f"Select a new mode:"
        ),
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^confirm_delete_"))
async def confirm_delete_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split("_")
    file_id_str = parts[2]
    item_type = parts[3] 

    collection = db.files if item_type == 'single' else db.multi_files
    record_to_delete = collection.find_one({"_id": file_id_str, "user_id": user_id})

    if not record_to_delete:
        await callback_query.answer(to_small_caps("File/Bundle not found or already deleted."), show_alert=True)
        try:
             await callback_query.message.edit_text(to_small_caps("❌ Item could not be deleted. It might be a bad link or already gone."))
        except Exception:
             pass
        return

    try:
        if item_type == 'single':
            message_ids_to_delete = [record_to_delete['message_id']]
        else:
            message_ids_to_delete = record_to_delete['message_ids']
            
        chunk_size = 100 
        for i in range(0, len(message_ids_to_delete), chunk_size):
            chunk = message_ids_to_delete[i:i + chunk_size]
            await client.delete_messages(chat_id=LOG_CHANNEL, message_ids=chunk)
            await asyncio.sleep(0.5)
            
        collection.delete_one({"_id": file_id_str})

        await callback_query.answer(to_small_caps(f"Item deleted successfully! ID: {file_id_str}"), show_alert=True)
        await callback_query.message.edit_text(to_small_caps(f"✅ The {item_type.upper()} item `{record_to_delete.get('file_name', 'Unnamed Item')}` has been permanently deleted."))
        
        log_text = to_small_caps(
            f"🗑️ Item Deleted\n"
            f"• User: {await get_user_full_name(callback_query.from_user)} (`{user_id}`)\n"
            f"• Type: `{item_type.upper()}`\n"
            f"• ID: `{file_id_str}`"
        )
        await client.send_message(LOG_CHANNEL, log_text)
        
    except Exception as e:
        logging.error(f"Failed to delete item {file_id_str}: {e}", exc_info=True)
        if "MESSAGE_DELETE_FORBIDDEN" in str(e) or "MESSAGE_NOT_FOUND" in str(e):
             collection.delete_one({"_id": file_id_str})
             await callback_query.answer(to_small_caps("Item deleted from database, but message removal from log channel failed."), show_alert=True)
             await callback_query.message.edit_text(to_small_caps(f"✅ The {item_type.upper()} item `{record_to_delete.get('file_name', 'Unnamed Item')}` has been deleted from the database."))
        else:
             await callback_query.answer(to_small_caps("An error occurred while deleting the item."), show_alert=True)
             await callback_query.message.edit_text(to_small_caps("❌ An error occurred while trying to delete the item. Please try again later."))

@app.on_callback_query(filters.regex(r"^cancel_delete"))
async def cancel_delete_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer(to_small_caps("Deletion cancelled."), show_alert=True)
    await callback_query.message.edit_text(to_small_caps("↩️ Deletion cancelled. Your file/bundle is safe."))

@app.on_inline_query()
async def inline_search(client, inline_query):
    query = inline_query.query.strip().lower()
    
    if not query:
        results = [
            InlineQueryResultArticle(
                title=to_small_caps("🔍 Search for a file/bundle"),
                description=to_small_caps("Type a filename or keyword to find your links."),
                input_message_content=InputTextMessageContent(
                    message_text=to_small_caps("🤔 Searching for files...")
                )
            )
        ]
        await client.answer_inline_query(inline_query.id, results, cache_time=0)
        return

    single_files_found = list(db.files.find(
        {"user_id": inline_query.from_user.id, "file_name": {"$regex": query, "$options": "i"}}
    ).limit(7))
    
    multi_files_found = list(db.multi_files.find(
        {"user_id": inline_query.from_user.id, "file_name": {"$regex": query, "$options": "i"}}
    ).limit(7))
    
    all_found = single_files_found + multi_files_found
    all_found.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    
    articles = []
    bot_username = (await client.get_me()).username
    
    for item_record in all_found[:15]:
        file_id_str = item_record['_id']
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        is_single = 'message_id' in item_record
        item_type = "File" if is_single else "Bundle"
        file_name = item_record.get('file_name', f"Unnamed {item_type}")
        
        description = to_small_caps(f"{item_type} Link. Click to share.")
        if not is_single:
             description = to_small_caps(f"Bundle of {len(item_record.get('message_ids', []))} files. Click to share.")

        articles.append(
            InlineQueryResultArticle(
                title=to_small_caps(f"[{item_type}] {file_name}"),
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=to_small_caps(f"🔗 Here is the {item_type} link:\n`{share_link}`"),
                    disable_web_page_preview=True
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(to_small_caps(f"📤 Share {item_type}"), url=f"https://t.me/share/url?url={urllib.parse.quote(share_link)}")]])
            )
        )
        
    if not articles:
        articles.append(
            InlineQueryResultArticle(
                title=to_small_caps("❌ No Files Found"),
                description=to_small_caps(f"No files or bundles matching '{query}' were found in your uploads."),
                input_message_content=InputTextMessageContent(
                    message_text=to_small_caps("😔 No matching files found. Try a different keyword or upload files first.")
                )
            )
        )

    await client.answer_inline_query(
        inline_query.id,
        results=articles,
        cache_time=5
    )

# --- Group Moderation Features ---

@app.on_message(filters.group & ~filters.service)
async def group_moderation_handler(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot or message.from_user.id in ADMINS:
         return
         
    text_with_caption = message.text or message.caption
    
    # Anti-Link Filter
    if message.entities or message.caption_entities:
        entities = message.entities if message.entities else message.caption_entities
        for entity in entities:
            if entity.type in ["url", "text_link", "text_mention"]:
                try:
                    await message.delete()
                    await message.reply(
                        to_small_caps(f"🚫 Link Removed! {await get_user_full_name(message.from_user)}, unauthorized links are not allowed here."),
                        quote=True
                    )
                    log_text = to_small_caps(
                        f"🔗 Link Removed!\n"
                        f"• User: {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
                        f"• Group: {message.chat.title} (`{message.chat.id}`)\n"
                        f"• Entity Type: `{entity.type}`"
                    )
                    if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)
                    return
                except ChatAdminRequired:
                    return

    # Anti-Badwords Filter
    text_lower = (text_with_caption or "").lower()
    for badword in BADWORDS:
        if badword and badword in text_lower:
            try:
                await message.delete()
                warnings_record = db.warnings.find_one({"user_id": message.from_user.id, "chat_id": message.chat.id})
                new_warnings = warnings_record['warnings'] + 1 if warnings_record else 1
                db.warnings.update_one(
                     {"user_id": message.from_user.id, "chat_id": message.chat.id}, 
                     {"$set": {"warnings": new_warnings, "last_warned": datetime.now(timezone.utc)}},
                     upsert=True
                )
                
                reply_message = to_small_caps(f"🤬 Censored! Please mind your language, {await get_user_full_name(message.from_user)}. ({new_warnings}/{MAX_WARNINGS} Warnings)")
                await message.reply(reply_message, quote=True)
                
                log_text = to_small_caps(
                    f"🤬 Badword Removed!\n"
                    f"• User: {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
                    f"• Group: {message.chat.title} (`{message.chat.id}`)\n"
                    f"• Warns: `{new_warnings}` / `{MAX_WARNINGS}`"
                )
                if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)

                if new_warnings >= MAX_WARNINGS:
                    await client.restrict_chat_member(
                        message.chat.id, 
                        message.from_user.id, 
                        permissions=ChatPermissions(can_send_messages=False), 
                        until_date=datetime.now(timezone.utc) + timedelta(hours=24)
                    )
                    db.warnings.delete_one({"user_id": message.from_user.id, "chat_id": message.chat.id})
                    await message.reply(to_small_caps(f"🚫 {await get_user_full_name(message.from_user)} reached max warnings and has been muted for 24 hours."))

                return
            except ChatAdminRequired:
                return


@app.on_message(filters.command("warn") & filters.group & filters.user(ADMINS))
async def warn_user(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply(to_small_caps("⚠️ Please reply to a user's message to warn them."))
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply(to_small_caps("Cannot warn a bot or an admin/owner."))
        return
        
    warnings_record = db.warnings.find_one({"user_id": target_user.id, "chat_id": chat_id})
    if warnings_record:
        new_warnings = warnings_record['warnings'] + 1
        db.warnings.update_one({"user_id": target_user.id, "chat_id": chat_id}, {"$set": {"warnings": new_warnings, "last_warned": datetime.now(timezone.utc)}})
    else:
        new_warnings = 1
        db.warnings.insert_one({"user_id": target_user.id, "chat_id": chat_id, "warnings": new_warnings, "last_warned": datetime.now(timezone.utc)})
    
    await message.reply(
        to_small_caps(
            f"⚠️ {await get_user_full_name(target_user)} has been warned by Admin. "
            f"Warnings: {new_warnings}/{MAX_WARNINGS}."
        )
    )
    
    log_text = to_small_caps(
        f"⚠️ User Warned!\n"
        f"• User: {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
        f"• Admin: {await get_user_full_name(message.from_user)}\n"
        f"• Group: {message.chat.title}\n"
        f"• New Warnings: `{new_warnings}`"
    )
    if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)
    
    if new_warnings >= MAX_WARNINGS:
        try:
            await client.restrict_chat_member(
                chat_id, 
                target_user.id, 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=datetime.now(timezone.utc) + timedelta(hours=24)
            )
            db.warnings.delete_one({"user_id": target_user.id, "chat_id": chat_id})
            await message.reply(to_small_caps(f"🚫 {await get_user_full_name(target_user)} received {MAX_WARNINGS} warnings and has been muted for 24 hours."))
            
        except ChatAdminRequired:
            await message.reply(to_small_caps("I need admin rights with 'Restrict users' permission to mute this user."))

@app.on_message(filters.command("mute") & filters.group & filters.user(ADMINS))
async def temp_mute(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply(to_small_caps("🔇 Please reply to a user's message to mute them. Example: /mute 30m or /mute 1h ."))
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply(to_small_caps("Cannot mute a bot or an admin."))
        return
        
    try:
        duration_str = message.command[1].lower() if len(message.command) >= 2 else "1h"
        duration_unit = duration_str[-1]
        duration_value = int(duration_str[:-1])

        if duration_unit == "m":
            unmute_time = datetime.now(timezone.utc) + timedelta(minutes=duration_value)
            duration_text = f"{duration_value} minutes"
        elif duration_unit == "h":
            unmute_time = datetime.now(timezone.utc) + timedelta(hours=duration_value)
            duration_text = f"{duration_value} hours"
        elif duration_unit == "d":
            unmute_time = datetime.now(timezone.utc) + timedelta(days=duration_value)
            duration_text = f"{duration_value} days"
        else:
            await message.reply(to_small_caps("Invalid duration format. Use /mute <value>m/h/d (e.g., /mute 10m , /mute 1h )."))
            return

        await client.restrict_chat_member(
            chat_id, 
            target_user.id, 
            permissions=ChatPermissions(can_send_messages=False), 
            until_date=unmute_time
        )
        await message.reply(to_small_caps(f"🔇 {await get_user_full_name(target_user)} has been muted for {duration_text}."))
        
        log_text = to_small_caps(
            f"🔇 User Muted!\n"
            f"• User: {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
            f"• Admin: {await get_user_full_name(message.from_user)}\n"
            f"• Group: {message.chat.title}\n"
            f"• Duration: `{duration_text}`"
        )
        if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)

    except (IndexError, ValueError):
        await message.reply(to_small_caps("Please provide a valid duration. Example: /mute 30m ."))
    except ChatAdminRequired:
        await message.reply(to_small_caps("I need admin rights with 'Restrict users' permission to mute this user."))

@app.on_message(filters.command("unmute") & filters.group & filters.user(ADMINS))
async def unmute_user(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply(to_small_caps("🔊 Please reply to a user's message to unmute them."))
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply(to_small_caps("Cannot unmute a bot or an admin."))
        return
        
    try:
        await client.restrict_chat_member(
            chat_id, 
            target_user.id, 
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True
            ),
            until_date=datetime.now(timezone.utc)
        )
        await message.reply(to_small_caps(f"🔊 {await get_user_full_name(target_user)} has been unmuted."))
        
        log_text = to_small_caps(
            f"🔊 User Unmuted!\n"
            f"• User: {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
            f"• Admin: {await get_user_full_name(message.from_user)}\n"
            f"• Group: {message.chat.title}"
        )
        if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)

    except ChatAdminRequired:
        await message.reply(to_small_caps("I need admin rights with 'Restrict users' permission to unmute this user."))

@app.on_message(filters.command("kick") & filters.group & filters.user(ADMINS))
async def temp_kick(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply(to_small_caps("👢 Please reply to a user's message to kick them."))
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply(to_small_caps("Cannot kick a bot or an admin."))
        return
        
    try:
        await client.kick_chat_member(chat_id, target_user.id)
        await client.unban_chat_member(chat_id, target_user.id)
        
        await message.reply(to_small_caps(f"👢 {await get_user_full_name(target_user)} has been kicked from the group."))
        
        log_text = to_small_caps(
            f"👢 User Kicked!\n"
            f"• User: {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
            f"• Admin: {await get_user_full_name(message.from_user)}\n"
            f"• Group: {message.chat.title}"
        )
        if GROUP_LOG_CHANNEL and GROUP_LOG_CHANNEL != 0: await client.send_message(GROUP_LOG_CHANNEL, log_text)
        
    except ChatAdminRequired:
        await message.reply(to_small_caps("I need admin rights with 'Ban users' permission to kick this user."))

# --- Main Bot Runner ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMINS is not set. Admin commands will not work.")
    if not FORCE_CHANNELS:
        logging.warning("⚠️ WARNING: FORCE_CHANNELS is not set. Force join feature will be disabled.")
    if not GROUP_LOG_CHANNEL:
        logging.warning("⚠️ WARNING: GROUP_LOG_CHANNEL is not set. Group logs will not be saved.")
        
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting with JSON Database...")
    app.run()
    logging.info("Bot has stopped.")
