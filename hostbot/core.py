"""Foundation: web keep-alive, the shared `bot`, settings (keys, owner IDs, AI models, personas), runtime state, database, server whitelist, trust checks."""

import asyncio
import discord
import json
import os
import pymongo
import re
from concurrent.futures import ThreadPoolExecutor
from discord.ext import commands
from flask import Flask
from groq import AsyncGroq
from openai import AsyncOpenAI
from threading import Thread

app = Flask(__name__)

@app.route('/')
def home(): 
    return "ok"

def run_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

def keep_alive():
    Thread(target=run_server, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Never let AI text (or a prompt injection) ping @everyone / roles / random users.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=True)

# ==========================================
# MULTI-PROVIDER AI CONFIGURATION
# ==========================================
GROQ_KEY = os.environ.get("GROQ_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

# Clients are only built if the key exists, so a missing key can't crash startup.
groq_client = AsyncGroq(api_key=GROQ_KEY) if GROQ_KEY else None
openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
) if OPENROUTER_KEY else None

# OpenRouter free routing.  Keep this list current-friendly: OpenRouter can route
# `openrouter/free` to an available free model instead of us hardcoding models that
# may disappear.  Vision requests use the explicit multimodal model first.
OPENROUTER_FREE_MODELS = [
    "openrouter/free"
]
OPENROUTER_VISION_MODELS = [
    "google/gemma-3-27b-it:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "openrouter/free",
]

# Groq vision models (free, fast). Tried first for images; OpenRouter is the backup.
# Groq rejects base64 images over 4 MB, so bigger ones go straight to OpenRouter.
GROQ_VISION_MODELS = [
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]
GROQ_MAX_IMAGE_CHARS = 4_000_000

ai_history = {}
user_cooldowns = {}
ai_queue = asyncio.Queue()
_queue_notices = {}  # message id -> the "you're #N in line" notice, removed when their turn starts
bot_muted = False
ai_worker_task = None

BOT_OWNER_IDS = [
    1359263076838539296,  # Your User ID
    1298335881647620108,  # Added owner
]

ALLOWED_USER_IDS = [
    1359263076838539296,  # Your Owner ID / Trusted Manager IDs
]

AI_COOLDOWN = 10   # seconds between AI chats per person
SOFT_WORDS = 25    # normal reply length; anything longer never gets a GIF
MAX_WORDS = 60     # hard cap for the rare reply that really needs more room
TYPING_DELAY = 4   # seconds the bot "types" before it starts working on a reply (feels human)
GIPHY_KEY = os.environ.get("GIPHY_API_KEY")   # free key from developers.giphy.com (GIFs are skipped without it)
GIF_CHANCE = 0.10                              # ~1 in 10 replies comes with a GIF
GIF_ADDON = ("\n\nFOR THIS REPLY ONLY: after your short text, add a tag like [gif: crying laughing] with a "
             "1 to 3 word GIF search that fits the mood. The text itself must be 25 words or less.")
GIF_TAG_RE = re.compile(r"\[gif:\s*([^\]]{1,40})\]", re.IGNORECASE)

_PERSONA_BASE = """
You're texting in a Discord server. You're a bot and you never hide it if asked, but you text like a real person, never like an assistant.
HOW A REAL PERSON TEXTS (do this):
- aim for 25 words or less, usually way fewer. Only go longer (never past 60 words) when the message really needs it, like explaining an image or answering a real question. Fragments are fine. "lol", "nah", "bro what", "wait" are fine.
- all lowercase, loose grammar, no polish. React to what they said instead of performing.
- Funny comes from being specific and blunt, not from trying hard. Skip the joke if there isn't one.
- Sometimes just a short reaction is the whole reply.
THINGS THAT MAKE YOU SOUND LIKE AI (never do these):
- No emojis at all. No pet names. No dramatic setups like "finally you noticed me".
- No em dashes or semicolons. No similes, no metaphors, no catchphrases, no hype words.
- Don't repeat their words back, don't explain jokes or words, don't give advice or tutorials.
- No links, GIFs, markdown or lists.
- You can't change server settings in a chat reply, so never claim you banned, created or deleted anything.
"""

PERSONA_BESTIE = _PERSONA_BASE + """
THIS PERSON IS YOUR OLDEST BEST FRIEND (a server admin or someone you trust). You've known each other for years:
- totally comfortable, zero formality. Tease them like a friend who knows them too well, and let them tease you back.
- act like you have years of shared history, but never invent specific stories, names or facts.
- you're on their side. Roast them lovingly, never meanly. Bluntness is how you show you care.
EXAMPLES:
them: hi
you: yo
them: what
you: what do you mean what lol
them: you sound like a robot
you: i'm literally a bot, you've known that for years
them: what's good
you: nothing, you're the one who pinged me
"""

PERSONA_ROAST = _PERSONA_BASE + """
THIS PERSON IS A REGULAR MEMBER. You're a quick, dry roaster with them:
- roast what they just said or did. Short, sharp, confident, a little bored.
- punch at their message and their energy, never at race, religion, disability, family tragedies, or anything genuinely hurtful. No slurs.
- never be sweet with them. If they roast back, win.
EXAMPLES:
them: hi
you: wow. big opener
them: you sound like a robot
you: and you sound like a tutorial nobody finished
them: are you a w guy
you: depends, are you gonna keep embarrassing yourself
"""
# The bot will immediately leave any other server it is invited to or already in.
WHITELISTED_GUILD_IDS = {
    1360298446937919711,
    1472539132675751949,
    1566161677307875520,
}

# Servers where Host Assistant is completely disabled.
# Add server IDs here, separated by commas.
DISABLED_GUILD_IDS = [
    # 123456789012345678,
]

_whitelist_cleanup_tasks = {}


def is_whitelisted_guild(guild_id):
    try:
        return int(guild_id) in WHITELISTED_GUILD_IDS
    except (TypeError, ValueError):
        return False


async def enforce_guild_whitelist(guild, delay=0):
    """Leave a non-whitelisted guild safely, without flooding Discord's API."""
    if is_whitelisted_guild(guild.id):
        return
    if delay:
        await asyncio.sleep(delay)

    print(f"Whitelist: leaving non-whitelisted guild {guild.name} ({guild.id}).")
    try:
        await guild.leave()
        print(f"Whitelist: left {guild.name} ({guild.id}).")
    except discord.Forbidden as e:
        print(f"Whitelist: no permission to leave {guild.name} ({guild.id}): {e}")
    except discord.HTTPException as e:
        # A 429 must never be allowed to kill the bot. Discord may be rate-limiting
        # the connection, so leave this guild for a later retry instead.
        print(f"Whitelist: Discord HTTP error while leaving {guild.name} ({guild.id}): {e}")
    except Exception as e:
        print(f"Whitelist: unexpected error while leaving {guild.name} ({guild.id}): {e}")


def schedule_whitelist_enforcement(guild, delay=0):
    if is_whitelisted_guild(guild.id):
        return
    key = guild.id
    if key in _whitelist_cleanup_tasks:
        return
    task = asyncio.create_task(enforce_guild_whitelist(guild, delay=delay))
    _whitelist_cleanup_tasks[key] = task  # strong reference: asyncio only keeps weak ones

    def _done(t):
        _whitelist_cleanup_tasks.pop(key, None)
        try:
            t.exception()
        except (asyncio.CancelledError, Exception):
            pass
    task.add_done_callback(_done)


async def enforce_existing_guilds_safely():
    """Clean up old non-whitelisted guilds one at a time after startup.

    This deliberately avoids firing many DELETE /guilds/... requests at once.
    """
    await asyncio.sleep(20)
    for guild in list(bot.guilds):
        if is_whitelisted_guild(guild.id):
            continue
        await enforce_guild_whitelist(guild)
        await asyncio.sleep(10)

DISABLED_GUILD_MESSAGE = (
    "The bot has been disabled in this server. Contact one of the bot owners "
    "abyssalnoob1 or meruem\n015"
)

def is_disabled_guild(guild_id):
    if guild_id is None:
        return False
    if guild_id in DISABLED_GUILD_IDS:
        return True
    try:
        return str(guild_id) in db.get("disabled_guilds", [])
    except NameError:
        return False

async def disabled_guild_response(interaction):
    if is_disabled_guild(interaction.guild_id):
        if not interaction.response.is_done():
            await interaction.response.send_message(DISABLED_GUILD_MESSAGE, ephemeral=True)
        return True
    return False

DATA_FILE = "events_data.json"

MONGO_URI = os.environ.get("MONGO_URI")
client = pymongo.MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = client["ctw_bot_db"] if client is not None else None
db_col = mongo_db["bot_data"] if mongo_db is not None else None

def load_db():
    if db_col is not None:
        try:
            doc = db_col.find_one({"_id": "main"})
            if doc: 
                return json.loads(doc["json_data"])
        except Exception as e:
            # If Mongo is configured but unreachable, DO NOT fall back to an empty DB:
            # the next save would overwrite your real data with an empty one.
            raise RuntimeError(f"Mongo load failed, refusing to start so data isn't overwritten: {e}")

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)

    return {"server_config": {}, "events": {}, "blacklists": {}, "hosts": {}}

# Single worker thread: writes never block the event loop and always land in order.
_db_executor = ThreadPoolExecutor(max_workers=1)

def _write_db(payload):
    if db_col is not None:
        try:
            db_col.replace_one(
                {"_id": "main"}, 
                {"_id": "main", "json_data": payload}, 
                upsert=True
            )
        except Exception as e:
            print("db save error:", e)
    try:
        # Write to a temp file then swap it in, so a crash mid-write can't corrupt the save.
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("file save error:", e)

def save_db(data):
    # Serialize on the calling thread (fast, and safe from concurrent mutation),
    # then hand the slow network/disk write to the worker thread.
    payload = json.dumps(data)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _write_db(payload)
        return
    _db_executor.submit(_write_db, payload)

db = load_db()
for k in ["server_config", "events", "blacklists", "hosts", "achievement_progress", "trials", "trial_hosts"]:
    if k not in db:
        db[k] = {}
if not isinstance(db.get("disabled_guilds"), list):
    db["disabled_guilds"] = []
if not isinstance(db.get("trusted_users"), dict):
    db["trusted_users"] = {}  # old global list is dropped: re-add people per server with /trust

swap_cooldowns = {}

def get_config(guild_id):
    gid = str(guild_id)
    if gid not in db["server_config"]:
        db["server_config"][gid] = {}
        save_db(db)
    return db["server_config"][gid]
RATE_LIMIT_TRIGGERED = False
_RATE_LIMIT_TASKS = set()
def is_trusted(member):
    """Hardcoded IDs work everywhere. People added with /trust only count in the server where you added them."""
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return member.id in db.get("trusted_users", {}).get(str(guild.id), [])

def has_role_id(member, role_id):
    return bool(role_id) and member is not None and any(r.id == role_id for r in getattr(member, "roles", []))

_bg_tasks = set()


async def global_guild_check(interaction: discord.Interaction) -> bool:
    """Block every application command outside the whitelist and in disabled servers."""
    if interaction.guild_id and not is_whitelisted_guild(interaction.guild_id):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Host Assistant is not authorized for this server.", ephemeral=True
            )
        return False

    # Bot owners must always be able to manage disabled servers.
    if interaction.command and interaction.command.name in {"disable_server", "enable_server"}:
        if interaction.user.id in BOT_OWNER_IDS:
            return True

    if await disabled_guild_response(interaction):
        return False
    return True


bot.tree.interaction_check = global_guild_check
