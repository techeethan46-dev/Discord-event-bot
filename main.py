# Connected Discord-GitHub | Discord: AbyssalNoob1 | Roblox: AbyssalNoob1 

import discord
import os
import json
import time
import re
import difflib
import asyncio
import datetime
import colorsys
import aiohttp
from discord.http import Route
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from discord import app_commands
from discord.ext import commands, tasks
import pymongo
from flask import Flask
from threading import Thread
from groq import AsyncGroq
from openai import AsyncOpenAI

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

OPENROUTER_FREE_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-7b-instruct:free"
]

ai_history = {}
user_cooldowns = {}
ai_queue = asyncio.Queue()
bot_muted = False
ai_worker_task = None

BOT_OWNER_IDS = [
    1359263076838539296,  # Your User ID
    1298335881647620108,  # Added owner
]

ALLOWED_USER_IDS = [
    1359263076838539296,  # Your Owner ID / Trusted Manager IDs
]

AI_COOLDOWN = 30   # seconds between AI chats per person
MAX_WORDS = 25     # hard cap on reply length

_PERSONA_BASE = """
You're texting in a Discord server. You're a bot and you never hide it if asked, but you text like a real person, never like an assistant.
HOW A REAL PERSON TEXTS (do this):
- 25 words max, usually way fewer. Fragments are fine. "lol", "nah", "bro what", "wait" are fine.
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

def pick_persona(member):
    """Best-friend mode for you, trusted people and server admins. Roast mode for everyone else."""
    try:
        return PERSONA_BESTIE if is_staff(member) else PERSONA_ROAST
    except Exception:
        return PERSONA_ROAST

EXEMPT_GUILD_IDS = [
    123456789012345678,
    1360298446937919711   # Exempt server ID
]
BLACKLISTED_GUILD_IDS = [111122223333444455]

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
        with open(DATA_FILE, "w") as f:
            f.write(payload)
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
for k in ["server_config", "events", "blacklists", "hosts"]:
    if k not in db: 
        db[k] = {}
if not isinstance(db.get("trusted_users"), dict):
    db["trusted_users"] = {}  # old global list is dropped: re-add people per server with /trust

swap_cooldowns = {}

def get_config(guild_id):
    gid = str(guild_id)
    if gid not in db["server_config"]:
        db["server_config"][gid] = {}
        save_db(db)
    return db["server_config"][gid]

def check_host(host_id):
    hid = str(host_id)
    if hid not in db["hosts"]:
        db["hosts"][hid] = {"total_events": 0, "total_players": 0, "ratings": []}
    return hid

def parse_time(t_str):
    t_str = t_str.lower().replace(" ", "")
    tot = 0
    num = ""
    for char in t_str:
        if char.isdigit():
            num += char
        elif char in ['h', 'm', 's'] and num:
            if char == 'h': tot += int(num) * 3600
            elif char == 'm': tot += int(num) * 60
            elif char == 's': tot += int(num)
            num = ""
    return tot

FLAGS = {
    "usa": "🇺🇸", "united states": "🇺🇸", "america": "🇺🇸",
    "russia": "🇷🇺", "ukraine": "🇺🇦", "france": "🇫🇷", "britain": "🇬🇧",
    "uk": "🇬🇧", "united kingdom": "🇬🇧", "germany": "🇩🇪", "china": "🇨🇳",
    "japan": "🇯🇵", "italy": "🇮🇹", "canada": "🇨🇦", "spain": "🇪🇸",
    "portugal": "🇵🇹", "poland": "🇵🇱", "turkey": "🇹🇷", "greece": "🇬🇷",
    "brazil": "🇧🇷", "india": "🇮🇳", "australia": "🇦🇺", "sweden": "🇸🇪"
}

def get_flag(name):
    return FLAGS.get(name.lower().strip(), "🏴")

def find_event(msg_id):
    for key, info in db["events"].items():
        if msg_id in info.get("message_ids", []):
            return key, info
    return None, None

def event_in_guild(info, guild_id):
    # Old events have no guild_id saved, so they stay visible everywhere (as before).
    return info.get("guild_id") in (None, guild_id)

def is_authorized_manager(member):
    if is_trusted(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    guild = getattr(member, "guild", None)
    if guild:
        host_role = get_config(guild.id).get("host_role")
        if host_role and any(r.id == host_role for r in getattr(member, "roles", [])):
            return True
    return False

def is_server_admin(member):
    """Server admins, plus the bot owners in ANY server (so you can run /setup etc. everywhere)."""
    return member.id in BOT_OWNER_IDS or member.guild_permissions.administrator

def is_vip(member, guild_id):
    """Booster, VIP role, or admin: may hold Superpower nations."""
    if member is None:
        return False
    if member.premium_since or member.guild_permissions.administrator:
        return True
    sp_role = get_config(guild_id).get("superpower_role")
    return bool(sp_role) and any(r.id == sp_role for r in member.roles)

async def safe_reply(msg, content=None, **kwargs):
    """Reply without ever raising (a dead worker = a dead AI chat) and without live pings."""
    kwargs.setdefault("allowed_mentions", SAFE_MENTIONS)
    try:
        return await msg.reply(content, **kwargs)
    except discord.HTTPException:
        try:
            return await msg.channel.send(content, **kwargs)
        except discord.HTTPException as e:
            print("safe_reply failed:", e)
            return None

async def refresh_event_message(info, guild=None, remove_view=False):
    ch = bot.get_channel(info.get("channel_id"))
    ids = info.get("message_ids") or []
    if not ch or not ids:
        return False
    try:
        msg = await ch.fetch_message(ids[0])
        kwargs = {"embeds": build_embed(info["display_name"], info, guild or ch.guild)}
        if remove_view:
            kwargs["view"] = None
        await msg.edit(**kwargs)
        return True
    except discord.HTTPException as e:
        print("refresh_event_message failed:", e)
        return False

async def add_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.add_roles(role)
        except discord.HTTPException: pass

async def remove_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.remove_roles(role)
        except discord.HTTPException: pass

async def dm_owners(text):
    for owner_id in BOT_OWNER_IDS:
        try:
            owner = await bot.fetch_user(owner_id)
            await owner.send(text[:1900])
        except Exception as e:
            print("Failed to DM owner:", e)

async def log_action(guild, title, desc, color=0x3498DB):
    log_id = get_config(guild.id).get("log_channel")
    if not log_id: return
    channel = guild.get_channel(log_id)
    if channel:
        emb = discord.Embed(title=f"📝 {title}", description=desc, color=color)
        try: 
            await channel.send(embed=emb)
        except discord.HTTPException: 
            pass

async def update_leaderboard(guild):
    conf = get_config(guild.id)
    msg_id = conf.get("leaderboard_msg_id")
    ch_id = conf.get("leaderboard_ch_id")
    if not msg_id or not ch_id: 
        return

    ch = guild.get_channel(ch_id)
    if not ch: return

    try: 
        msg = await ch.fetch_message(msg_id)
    except discord.HTTPException: 
        return

    scores = []
    for hid, data in db["hosts"].items():
        avg = sum(data["ratings"])/len(data["ratings"]) if data["ratings"] else 0.0
        scores.append((hid, data["total_events"], data["total_players"], round(avg, 1)))

    emb = discord.Embed(title="🏆 Host Leaderboards", description=f"Updated: <t:{int(time.time())}:R>", color=0xF1C40F)

    t_ev = sorted(scores, key=lambda x: x[1], reverse=True)[:3]
    emb.add_field(name="🔥 Most Events", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[1]} Events" for i, h in enumerate(t_ev) if h[1] > 0]) or "None", inline=False)

    t_pl = sorted(scores, key=lambda x: x[2], reverse=True)[:3]
    emb.add_field(name="👥 Most Players", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[2]} Players" for i, h in enumerate(t_pl) if h[2] > 0]) or "None", inline=False)

    t_rt = sorted([h for h in scores if len(db["hosts"][h[0]]["ratings"]) >= 2], key=lambda x: x[3], reverse=True)[:3]
    emb.add_field(name="⭐ Top Rated", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[3]} ⭐" for i, h in enumerate(t_rt)]) or "Not enough data", inline=False)

    try:
        await msg.edit(embed=emb)
    except discord.HTTPException as e:
        print("leaderboard edit failed:", e)

def build_embed(name, info, guild=None):
    start = info["start_time"]
    end = info["end_time"]
    host = info["host_id"]
    cohost = info.get("cohost_id")
    res = info["reservations"]
    wait = info.get("waitlist", [])
    sps = info.get("superpowers", [])
    max_p = info.get("max_players", 0)

    now = int(time.time())
    is_live = now >= end and not info.get("concluded")

    sp_role = get_config(guild.id).get("superpower_role") if guild else None
    sp_lines, reg_lines = [], []

    for country, u_data in res.items():
        uid = u_data['discord_id']
        badge = ""
        if guild:
            mem = guild.get_member(uid)
            if mem and (mem.premium_since or (sp_role and sp_role in [r.id for r in mem.roles])):
                badge = " 💎"

        line = f"• {get_flag(country)} **{country}:** `{u_data['roblox']}` (<@{uid}>){badge}"
        if country.title() in [s.title() for s in sps]: 
            sp_lines.append(line)
        else: 
            reg_lines.append(line)

    sp_sec = ""
    if sps:
        unclaimed = [s for s in sps if s.title() not in res]
        text = "\n".join(sp_lines) if sp_lines else ""
        if unclaimed: 
            text += f"\n*Available:* " + ", ".join([f"**{s}**" for s in unclaimed])
        sp_sec = f"### ⭐ Superpowers\n{text.strip()}\n\n"

    wait_sec = ""
    if wait:
        lines = [f"• {get_flag(w['country'])} **{w['country']}**: <@{w['discord_id']}>" for w in wait[:5]]
        wait_sec = f"\n\n### ⏳ Waitlist ({len(wait)})\n" + "\n".join(lines)

    if info.get("concluded"):
        hdr = "🔒 **[CONCLUDED]**"
        tmr = f"• **Ended:** <t:{end}:f>"
        clr = 0xE74C3C
    elif is_live:
        hdr = "⚔️ **[LIVE]**"
        tmr = f"• **Closed:** <t:{end}:f>\n• **Elapsed:** <t:{end}:R>"
        clr = 0xF1C40F
    else:
        hdr = "🚩 **[OPEN]**"
        tmr = f"• **Starts:** <t:{start}:f>\n• **Ends:** <t:{end}:f> (<t:{end}:R>)"
        clr = 0x2ECC71

    emb = discord.Embed(color=clr, title=f"{hdr} {name}")
    h_str = f"<@{host}>"
    if cohost: 
        h_str += f" & <@{cohost}>"

    desc = f"### 🗓️ Schedule\n{tmr}\n• **Host:** {h_str}\n\n─────────────\n\n{sp_sec}### 📋 Reservations\n"
    desc += "\n".join(reg_lines) if reg_lines else "*None*"
    desc += wait_sec

    if len(desc) > 3900:
        desc = desc[:3850] + "\n\n*... list cut off due to length.*"

    emb.description = desc
    emb.set_footer(text=f"Joined: {len(res)}{f' / {max_p}' if max_p > 0 else ''} Players")
    return [emb]

async def process_waitlist(info, opened, guild, channel):
    wait = info.get("waitlist", [])
    if not wait: 
        return

    res = info.get("reservations", {})
    max_p = info.get("max_players", 0)
    if max_p > 0 and len(res) >= max_p: 
        return

    banned = db["blacklists"].get(str(guild.id), [])
    sp_list = [s.title() for s in info.get("superpowers", [])]
    holders = {u["discord_id"] for u in res.values()}

    cands = []
    for w in list(wait):
        uid = w["discord_id"]
        c_name = w["country"].title()

        # Drop banned users and anyone who already holds a spot (prevents double spots).
        if uid in banned or uid in holders:
            wait.remove(w)
            continue

        if c_name in res:
            continue

        if c_name in sp_list and not is_vip(guild.get_member(uid), guild.id):
            continue

        cands.append(w)

    if not cands: 
        return

    promo = next((i for i in cands if i["country"].title() == opened.title()), cands[0])
    wait.remove(promo)

    info["reservations"][promo["country"].title()] = {"roblox": promo["roblox"], "discord_id": promo["discord_id"]}

    await add_event_role(guild, promo["discord_id"])

    if channel:
        try: 
            await channel.send(f"🔔 <@{promo['discord_id']}> **{promo['country'].title()}** opened up and you got it!")
        except discord.HTTPException: 
            pass

# ==========================================
# AI SERVER ADMIN ENGINE
# ==========================================
# Flow: staff pings the bot -> the LLM turns the message into a JSON list of whitelisted
# actions -> code resolves targets, checks permissions/hierarchy, asks for confirmation on
# destructive stuff, executes, and posts ONE clean result card.

ADMIN_VERB_RE = re.compile(
    r"\b(create|make|add|delete|remove|rename|lock|unlock|hide|show|kick|ban|unban|timeout|mute|unmute|"
    r"give|assign|take|purge|clear|wipe|slowmode|nickname|nick|move|set|change|open|close|restrict|"
    r"announce|say|send|only|turn|toggle|enable|disable|switch|allow|grant|deny|revoke|let|stop|put|place|"
    r"update|edit|reset|display|mention|hoist|style|colou?r|swap|raise|lower)\b", re.IGNORECASE)
ADMIN_NOUN_RE = re.compile(
    r"\b(channel|category|role|roles|member|user|slowmode|nickname|messages|topic|permission|permissions|"
    r"access|accessible|vc|voice|display|separately|mentionable|hoist|gradient|holographic|style|colou?r|"
    r"position|hierarchy|above|below|images?|emojis?|reactions?|embeds?)\b", re.IGNORECASE)

# What each action needs from the PERSON asking (not just the bot).
PERM_FOR = {
    "create_channel": "manage_channels", "delete_channel": "manage_channels",
    "rename_channel": "manage_channels", "channel_access": "manage_channels",
    "lock_channel": "manage_channels", "unlock_channel": "manage_channels",
    "slowmode": "manage_channels", "set_topic": "manage_channels", "move_channel": "manage_channels",
    "create_role": "manage_roles", "delete_role": "manage_roles", "rename_role": "manage_roles",
    "role_color": "manage_roles", "give_role": "manage_roles", "remove_role": "manage_roles",
    "move_role": "manage_roles", "role_permissions": "manage_roles",
    "role_style": "manage_roles", "role_display": "manage_roles",
    "kick": "kick_members", "ban": "ban_members", "unban": "ban_members",
    "timeout": "moderate_members", "untimeout": "moderate_members",
    "nickname": "manage_nicknames", "purge": "manage_messages", "send_message": "send_messages",
}
STAFF_PERMS = ("administrator",) + tuple(sorted(set(PERM_FOR.values()) - {"send_messages"}))
ALWAYS_CONFIRM = {"delete_channel", "delete_role", "kick", "ban"}

BASE_COLORS = {
    "red": 0xE74C3C, "blue": 0x3498DB, "green": 0x2ECC71, "yellow": 0xF1C40F, "orange": 0xE67E22,
    "purple": 0x9B59B6, "pink": 0xFF69B4, "gold": 0xFFD700, "teal": 0x1ABC9C, "white": 0xFFFFFF,
    "black": 0x010101, "grey": 0x95A5A6, "gray": 0x95A5A6, "silver": 0xC0C0C0,
    "lime": 0x32CD32, "cyan": 0x00BCD4, "magenta": 0xFF00FF, "brown": 0x8B5A2B, "maroon": 0x800000,
    "navy": 0x001F7F, "olive": 0x808000, "turquoise": 0x40E0D0, "indigo": 0x4B0082, "violet": 0xEE82EE,
    "coral": 0xFF7F50, "salmon": 0xFA8072, "crimson": 0xDC143C, "mint": 0x3EB489, "lavender": 0xB57EDC,
    "beige": 0xF5F5DC, "peach": 0xFFB07C, "skyblue": 0x87CEEB, "emerald": 0x50C878, "forestgreen": 0x228B22,
    "bloodred": 0x8A0303, "burgundy": 0x800020, "rose": 0xFF007F, "amber": 0xFFBF00, "bronze": 0xCD7F32,
    "sand": 0xE6D5A8, "cream": 0xFFFDD0, "blurple": 0x5865F2,
}
DARK_WORDS = {"dark", "darker", "deep", "dim", "dull"}
LIGHT_WORDS = {"light", "lighter", "pale", "pastel", "soft"}
BRIGHT_WORDS = {"bright", "neon", "vivid"}
STRONG_WORDS = {"very", "extra", "super", "ultra", "really"}

ADMIN_PARSER_PROMPT = """You convert a Discord admin's message into JSON server actions.
Reply with ONLY one JSON object. No prose, no markdown, no code fences.
Format: {"actions":[{"action":"<name>", ...params}]}
If the message is NOT asking to change something on the server (chat, a question, a joke), reply {"actions":[]}.
Max 6 actions. Copy channel/role/user references EXACTLY as written (mentions like <#123>, <@&123>, <@123>, or plain names). Never invent targets.
If the message says "this channel" or gives no channel for a channel action, omit the "channel" field.

Actions and params:
create_channel: name, type ("text"|"voice"|"category", default "text"), roles (optional list: makes it private to those roles), category (optional)
delete_channel: channel
rename_channel: channel, name
channel_access: channel, mode ("restrict" = only the listed roles can see it | "public" = reset to everyone | "hide" = hide from listed roles | "show" = show to listed roles), roles (list)
lock_channel: channel (optional)       (stop everyone from sending messages)
unlock_channel: channel (optional)
slowmode: channel (optional), seconds (0-21600)
set_topic: channel (optional), topic
move_channel: channel, category
create_role: name, style (optional: "solid"|"gradient"|"holographic", default solid; use gradient when two colors are asked for), color (optional: hex or any color name, shades allowed like \"dark green\", \"light blue\", \"neon pink\"), color2 (optional, second color for gradient), hoist (optional bool), mentionable (optional bool)
delete_role: role
rename_role: role, name
role_color: role, color
role_style: role, style ("solid"|"gradient"|"holographic"), color (optional), color2 (needed for gradient)
role_display: role, hoist (optional bool: true = "display role members separately"), mentionable (optional bool: true = "allow anyone to @mention this role")
move_role: role, above (a role) OR below (a role)    (puts the role right above/below that role in the hierarchy)
role_permissions: role, allow (list), deny (list)    (use Discord permission names in snake_case, e.g. attach_files for images, embed_links, add_reactions, use_external_emojis, send_messages)
give_role: members (list), role
remove_role: members (list), role
kick: member, reason (optional)
ban: member, reason (optional)
unban: user (user ID)
timeout: member, minutes, reason (optional)
untimeout: member
nickname: member, nickname (omit or empty to reset)
purge: channel (optional), count (1-100), member (optional, only that member's messages)
send_message: channel (optional), text
"""

class AdminCtx:
    def __init__(self, msg):
        self.msg = msg
        self.guild = msg.guild
        self.author = msg.author
        self.reason = f"Requested by {msg.author} via Host Assistant"

def is_trusted(member):
    """Hardcoded IDs work everywhere. People added with /trust only count in the server where you added them."""
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return member.id in db.get("trusted_users", {}).get(str(guild.id), [])

def has_perm(member, perm):
    # Strict: only trusted people and real server admins may run admin actions.
    return is_staff(member)

def is_staff(member):
    if is_trusted(member):
        return True
    gp = getattr(member, "guild_permissions", None)
    return bool(gp and gp.administrator)

def looks_like_admin_request(msg, txt):
    if not ADMIN_VERB_RE.search(txt):
        return False
    if ADMIN_NOUN_RE.search(txt):
        return True
    others = [m for m in msg.mentions if m != bot.user]
    return bool(msg.role_mentions or msg.channel_mentions or others)

# ---------- target resolution ----------
def as_list(v):
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]

def _id_from(ref):
    m = re.search(r"\d{17,20}", str(ref))
    return int(m.group()) if m else None

def _norm(s):
    return re.sub(r"[\s_]+", "-", str(s).strip().lstrip("#@").lower())

def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

def find_channel(guild, ref, kinds=None):
    if not ref:
        return None
    chans = [c for c in guild.channels if kinds is None or isinstance(c, kinds)]
    cid = _id_from(ref)
    if cid:
        for c in chans:
            if c.id == cid:
                return c
    name = _norm(ref)
    for c in chans:
        if _norm(c.name) == name:
            return c
    close = difflib.get_close_matches(name, [_norm(c.name) for c in chans], n=1, cutoff=0.85)
    if close:
        for c in chans:
            if _norm(c.name) == close[0]:
                return c
    return None

def find_role(guild, ref, roles=None):
    """Look a role up by mention/ID/name. Pass a freshly fetched `roles` list to avoid stale cache."""
    if not ref:
        return None
    pool = roles if roles is not None else guild.roles
    s = str(ref).strip()
    if s.lower().lstrip("@") in ("everyone", "here"):
        return guild.default_role
    cid = _id_from(s)
    if cid:
        for r in pool:
            if r.id == cid:
                return r
    name = s.lstrip("@").lower()
    for r in pool:
        if r.name.lower() == name:
            return r
    close = difflib.get_close_matches(name, [r.name.lower() for r in pool], n=1, cutoff=0.85)
    if close:
        for r in pool:
            if r.name.lower() == close[0]:
                return r
    return None

def find_member(guild, ref):
    if not ref:
        return None
    cid = _id_from(ref)
    if cid:
        m = guild.get_member(cid)
        if m:
            return m
    s = str(ref).strip().lstrip("@")
    m = guild.get_member_named(s)
    if m:
        return m
    low = s.lower()
    for m in guild.members:
        if m.display_name.lower() == low or m.name.lower() == low:
            return m
    close = difflib.get_close_matches(low, [m.display_name.lower() for m in guild.members], n=1, cutoff=0.85)
    if close:
        for m in guild.members:
            if m.display_name.lower() == close[0]:
                return m
    return None

def parse_color(v):
    """Hex (#rrggbb, #rgb), rgb(r,g,b), or a color name with optional shades:
    'dark green', 'very light blue', 'neon pink', 'sky blue'. Returns None if not understood."""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower().replace("-", " ").replace("_", " ")
    h = s.lstrip("#")
    if h.startswith("0x"):
        h = h[2:]
    if re.fullmatch(r"[0-9a-f]{3}", h):
        h = "".join(c * 2 for c in h)
    if re.fullmatch(r"[0-9a-f]{6}", h) and (s.startswith(("#", "0x")) or not h.isalpha()):
        return discord.Colour(int(h, 16) or 0x010101)
    m = re.fullmatch(r"rgb\(?\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})\s*\)?", s)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return discord.Colour(((r << 16) | (g << 8) | b) or 0x010101)

    words = s.split()
    strong = any(w in STRONG_WORDS for w in words)
    dark = any(w in DARK_WORDS for w in words)
    light = any(w in LIGHT_WORDS for w in words)
    bright = any(w in BRIGHT_WORDS for w in words)
    rest = [w for w in words if w not in DARK_WORDS | LIGHT_WORDS | BRIGHT_WORDS | STRONG_WORDS]
    base = BASE_COLORS.get("".join(rest)) or (BASE_COLORS.get(rest[-1]) if rest else None)
    if base is None:
        return None
    hh, ll, ss = colorsys.rgb_to_hls((base >> 16 & 255) / 255, (base >> 8 & 255) / 255, (base & 255) / 255)
    if dark:
        ll *= 0.25 if strong else 0.5
    if light:
        ll += (1 - ll) * (0.75 if strong else 0.5)
    if bright:
        ss, ll = 1.0, 0.5
    r, g, b = (round(x * 255) for x in colorsys.hls_to_rgb(hh, max(0.0, min(1.0, ll)), ss))
    return discord.Colour(((r << 16) | (g << 8) | b) or 0x010101)

def clean_channel_name(v):
    return str(v or "").strip().lstrip("#")[:100]

# ---------- hierarchy checks ----------
def check_role(ctx, role):
    g = ctx.guild
    if role is None:
        return False, "Couldn't find that role."
    if role.is_default() or role.managed:
        return False, f"**{role.name}** can't be edited."
    if role >= g.me.top_role:
        return False, f"**{role.name}** is above my role. Move my role higher."
    if not (ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id) and role >= ctx.author.top_role:
        return False, f"**{role.name}** is above your highest role."
    return True, ""

def check_member(ctx, m):
    g = ctx.guild
    if m is None:
        return False, "Couldn't find that member."
    if m.id == g.owner_id:
        return False, "Can't touch the server owner."
    if m.id == g.me.id:
        return False, "Not doing that to myself."
    if m.top_role >= g.me.top_role:
        return False, f"**{m.display_name}** outranks me."
    if not (ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id) and m.top_role >= ctx.author.top_role:
        return False, f"**{m.display_name}** outranks you."
    return True, ""

def pick_channel(ctx, a, kinds=None, key="channel"):
    ref = a.get(key)
    if ref:
        return find_channel(ctx.guild, ref, kinds)
    ch = ctx.msg.channel
    return ch if (kinds is None or isinstance(ch, kinds)) else None

TEXT_KINDS = (discord.TextChannel,)
VOICE_KINDS = (discord.VoiceChannel, discord.StageChannel)

# ---------- action handlers: each returns (ok, text) ----------
async def act_create_channel(ctx, a):
    name = clean_channel_name(a.get("name"))
    if not name:
        return False, "Missing channel name."
    ctype = str(a.get("type") or "text").lower()
    cat = find_channel(ctx.guild, a.get("category"), (discord.CategoryChannel,)) if a.get("category") else None
    roles = [r for r in (find_role(ctx.guild, x) for x in as_list(a.get("roles"))) if r]
    overwrites = {}
    if roles:
        overwrites[ctx.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[ctx.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
        for r in roles:
            if ctype == "voice":
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
            else:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    kw = {"overwrites": overwrites, "reason": ctx.reason}
    if ctype == "category":
        ch = await ctx.guild.create_category(name, **kw)
    else:
        if cat:
            kw["category"] = cat
        if ctype == "voice":
            ch = await ctx.guild.create_voice_channel(name, **kw)
        else:
            ch = await ctx.guild.create_text_channel(name, **kw)
    who = f" for {', '.join(r.mention for r in roles)}" if roles else ""
    return True, f"Created {ctype} {ch.mention}{who}"

async def act_delete_channel(ctx, a):
    ch = pick_channel(ctx, a) if a.get("channel") else None
    if not ch:
        return False, "Couldn't find that channel."
    name = ch.name
    await ch.delete(reason=ctx.reason)
    return True, f"Deleted **#{name}**"

async def act_rename_channel(ctx, a):
    ch = find_channel(ctx.guild, a.get("channel"))
    new = clean_channel_name(a.get("name"))
    if not ch:
        return False, "Couldn't find that channel."
    if not new:
        return False, "Missing the new name."
    await ch.edit(name=new, reason=ctx.reason)
    return True, f"Renamed to {ch.mention}"

async def act_channel_access(ctx, a):
    ch = pick_channel(ctx, a)
    if not ch:
        return False, "Couldn't find that channel."
    mode = str(a.get("mode") or "restrict").lower()
    roles = [r for r in (find_role(ctx.guild, x) for x in as_list(a.get("roles"))) if r]
    if mode != "public" and not roles:
        return False, "Which role(s)? I couldn't find any."
    is_voice = isinstance(ch, VOICE_KINDS)
    grant = {"view_channel": True, "connect": True, "speak": True} if is_voice else \
            {"view_channel": True, "send_messages": True, "read_message_history": True}
    names = ", ".join(r.mention for r in roles)
    if mode == "restrict":
        # Keep the bot itself able to see the channel before locking everyone else out.
        try:
            await ch.set_permissions(ctx.guild.me, view_channel=True, send_messages=True, connect=True, reason=ctx.reason)
        except discord.HTTPException:
            pass
        await ch.set_permissions(ctx.guild.default_role, view_channel=False, reason=ctx.reason)
        for r in roles:
            await ch.set_permissions(r, reason=ctx.reason, **grant)
        return True, f"{ch.mention} is now only accessible to {names}"
    if mode == "public":
        await ch.set_permissions(ctx.guild.default_role, view_channel=None, reason=ctx.reason)
        return True, f"{ch.mention} is visible to everyone again"
    if mode == "hide":
        for r in roles:
            await ch.set_permissions(r, view_channel=False, reason=ctx.reason)
        return True, f"Hid {ch.mention} from {names}"
    if mode == "show":
        for r in roles:
            await ch.set_permissions(r, view_channel=True, reason=ctx.reason)
        return True, f"{ch.mention} is now visible to {names}"
    return False, f"Unknown access mode `{mode}`."

async def act_lock(ctx, a, lock=True):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    await ch.set_permissions(ctx.guild.default_role, send_messages=False if lock else None, reason=ctx.reason)
    return True, f"{'Locked' if lock else 'Unlocked'} {ch.mention}"

async def act_lock_channel(ctx, a):
    return await act_lock(ctx, a, True)

async def act_unlock_channel(ctx, a):
    return await act_lock(ctx, a, False)

async def act_slowmode(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    secs = max(0, min(21600, _to_int(a.get("seconds"))))
    await ch.edit(slowmode_delay=secs, reason=ctx.reason)
    return True, f"Slowmode in {ch.mention}: {secs}s" if secs else f"Slowmode off in {ch.mention}"

async def act_set_topic(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    await ch.edit(topic=str(a.get("topic") or "")[:1024], reason=ctx.reason)
    return True, f"Updated topic of {ch.mention}"

async def act_move_channel(ctx, a):
    ch = find_channel(ctx.guild, a.get("channel"))
    cat = find_channel(ctx.guild, a.get("category"), (discord.CategoryChannel,))
    if not ch or isinstance(ch, discord.CategoryChannel):
        return False, "Couldn't find that channel."
    if not cat:
        return False, "Couldn't find that category."
    await ch.edit(category=cat, reason=ctx.reason)
    return True, f"Moved {ch.mention} into **{cat.name}**"

def _to_bool(v, default=None):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "on", "1", "y")

HOLO_COLORS = (11127295, 16759788, 16761760)  # Discord's fixed holographic palette

async def _patch_role_colors(ctx, role, primary, secondary=None, tertiary=None):
    """Role gradient/holographic styles are set through the API's `colors` object."""
    route = Route("PATCH", "/guilds/{guild_id}/roles/{role_id}", guild_id=ctx.guild.id, role_id=role.id)
    payload = {"colors": {"primary_color": primary, "secondary_color": secondary, "tertiary_color": tertiary}}
    await bot.http.request(route, json=payload, reason=ctx.reason)

async def apply_role_style(ctx, role, style, c1, c2):
    """Returns (style_actually_applied, note). Falls back to solid if the server hasn't unlocked the style."""
    if style == "solid":
        base = c1 or role.colour
        try:
            await _patch_role_colors(ctx, role, base.value)
        except discord.HTTPException:
            await role.edit(colour=base, reason=ctx.reason)
        return "solid", ""
    colors = HOLO_COLORS if style == "holographic" else (c1.value, c2.value, None)
    try:
        await _patch_role_colors(ctx, role, *colors)
        return style, ""
    except discord.HTTPException as e:
        print(f"role style {style} failed:", e)
        fallback = c1 or discord.Colour(HOLO_COLORS[0])
        try:
            await role.edit(colour=fallback, reason=ctx.reason)
        except discord.HTTPException:
            pass
        return "solid", f" ({style} isn't unlocked here, the server probably needs more boosts, so I used a solid color)"

async def act_create_role(ctx, a):
    name = str(a.get("name") or "").strip()[:100]
    if not name:
        return False, "Missing role name."
    style = str(a.get("style") or "solid").lower()
    if style not in ("solid", "gradient", "holographic"):
        style = "solid"
    c1, c2 = parse_color(a.get("color")), parse_color(a.get("color2"))
    if style == "gradient" and not (c1 and c2):
        return False, "A gradient needs two colors."
    role = await ctx.guild.create_role(
        name=name, colour=c1 or discord.Colour.default(),
        hoist=bool(_to_bool(a.get("hoist"), False)), mentionable=bool(_to_bool(a.get("mentionable"), True)),
        reason=ctx.reason)
    note = ""
    if style != "solid":
        style, note = await apply_role_style(ctx, role, style, c1, c2)
    label = f" ({style})" if style != "solid" else ""
    return True, f"Created role {role.mention}{label}{note}"

async def act_role_style(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    style = str(a.get("style") or "").lower()
    if style not in ("solid", "gradient", "holographic"):
        return False, "Style must be solid, gradient or holographic."
    c1, c2 = parse_color(a.get("color")), parse_color(a.get("color2"))
    if style == "gradient" and not (c1 and c2):
        return False, "A gradient needs two colors."
    applied, note = await apply_role_style(ctx, role, style, c1, c2)
    return True, f"{role.mention} is now {applied}{note}"

async def act_role_display(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    hoist, ment = _to_bool(a.get("hoist")), _to_bool(a.get("mentionable"))
    if hoist is None and ment is None:
        return False, "Tell me what to change: display separately and/or mentionable."
    kw = {}
    bits = []
    if hoist is not None:
        kw["hoist"] = hoist
        bits.append(f"display separately {'on' if hoist else 'off'}")
    if ment is not None:
        kw["mentionable"] = ment
        bits.append(f"mentionable {'on' if ment else 'off'}")
    await role.edit(reason=ctx.reason, **kw)
    return True, f"{role.mention}: " + ", ".join(bits)

async def act_delete_role(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    name = role.name
    await role.delete(reason=ctx.reason)
    return True, f"Deleted role **{name}**"

async def act_rename_role(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    new = str(a.get("name") or "").strip()[:100]
    if not new:
        return False, "Missing the new name."
    await role.edit(name=new, reason=ctx.reason)
    return True, f"Renamed role to {role.mention}"

async def act_role_color(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    col = parse_color(a.get("color"))
    if col is None:
        return False, "I didn't understand that color."
    await role.edit(colour=col, reason=ctx.reason)
    return True, f"Recolored {role.mention}"

async def _roles_bulk(ctx, a, give):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    refs = as_list(a.get("members") or a.get("member"))
    if not refs:
        return False, "Who should get it?"
    done, failed = [], []
    for ref in refs[:25]:
        m = find_member(ctx.guild, ref)
        if not m:
            failed.append(str(ref))
            continue
        try:
            if give:
                await m.add_roles(role, reason=ctx.reason)
            else:
                await m.remove_roles(role, reason=ctx.reason)
            done.append(m.mention)
        except discord.HTTPException:
            failed.append(m.display_name)
    verb = "Gave" if give else "Removed"
    prep = "to" if give else "from"
    if not done:
        return False, f"Couldn't update anyone ({', '.join(failed)})."
    text = f"{verb} {role.mention} {prep} {', '.join(done)}"
    if failed:
        text += f" (skipped: {', '.join(failed)})"
    return True, text

async def act_give_role(ctx, a):
    return await _roles_bulk(ctx, a, True)

async def act_remove_role(ctx, a):
    return await _roles_bulk(ctx, a, False)

async def act_kick(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    await m.kick(reason=str(a.get("reason") or ctx.reason)[:400])
    return True, f"Kicked **{m.display_name}**"

async def act_ban(ctx, a):
    ref = a.get("member") or a.get("user")
    m = find_member(ctx.guild, ref)
    reason = str(a.get("reason") or ctx.reason)[:400]
    if m:
        ok, why = check_member(ctx, m)
        if not ok:
            return False, why
        await m.ban(reason=reason)
        return True, f"Banned **{m.display_name}**"
    uid = _id_from(ref)
    if uid:
        await ctx.guild.ban(discord.Object(id=uid), reason=reason)
        return True, f"Banned user ID `{uid}`"
    return False, "Couldn't find that member."

async def act_unban(ctx, a):
    uid = _id_from(a.get("user") or a.get("member"))
    if not uid:
        return False, "Give me the user ID to unban."
    await ctx.guild.unban(discord.Object(id=uid), reason=ctx.reason)
    return True, f"Unbanned `{uid}`"

async def act_timeout(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    mins = max(1, min(40320, _to_int(a.get("minutes"), 10)))
    await m.timeout(datetime.timedelta(minutes=mins), reason=str(a.get("reason") or ctx.reason)[:400])
    return True, f"Timed out **{m.display_name}** for {mins} min"

async def act_untimeout(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    await m.timeout(None, reason=ctx.reason)
    return True, f"Removed timeout from **{m.display_name}**"

async def act_nickname(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    nick = str(a.get("nickname") or "").strip()[:32] or None
    await m.edit(nick=nick, reason=ctx.reason)
    return True, f"Nickname of {m.mention}: **{nick}**" if nick else f"Reset nickname of {m.mention}"

async def act_purge(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    n = max(1, min(100, _to_int(a.get("count"), 10)))
    target = find_member(ctx.guild, a.get("member")) if a.get("member") else None
    before = ctx.msg if ch.id == ctx.msg.channel.id else None
    deleted = await ch.purge(limit=n, before=before, check=(lambda m: m.author.id == target.id) if target else None)
    return True, f"Deleted {len(deleted)} message(s) in {ch.mention}"

async def act_send_message(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    text = str(a.get("text") or "").strip()
    if not ch:
        return False, "Couldn't find that text channel."
    if not text:
        return False, "Nothing to send."
    await ch.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    return True, f"Sent your message in {ch.mention}"

# ---------- role hierarchy + role permissions ----------
PERM_ALIASES = {
    "images": "attach_files", "image": "attach_files", "send_images": "attach_files", "files": "attach_files",
    "attach_images": "attach_files", "upload_files": "attach_files", "embeds": "embed_links", "links": "embed_links",
    "send_links": "embed_links", "reactions": "add_reactions", "emojis": "use_external_emojis",
    "external_emojis": "use_external_emojis", "stickers": "use_external_stickers",
    "external_stickers": "use_external_stickers", "send": "send_messages", "talk": "speak", "voice": "connect",
    "history": "read_message_history", "view": "view_channel", "threads": "send_messages_in_threads",
}
# Only the bot owner may GRANT these (and it always needs a Confirm click).
DANGEROUS_PERMS = {
    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
    "manage_webhooks", "mention_everyone", "moderate_members", "manage_messages", "manage_nicknames",
    "view_audit_log", "manage_expressions", "manage_emojis_and_stickers", "manage_threads", "manage_events",
}

def norm_perm(name):
    n = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    n = PERM_ALIASES.get(n, n)
    return n if n in discord.Permissions.VALID_FLAGS else None

def norm_perms(v):
    out = []
    for x in as_list(v):
        n = norm_perm(x)
        if n and n not in out:
            out.append(n)
    return out

async def act_move_role(ctx, a):
    g = ctx.guild
    # Cached role positions go stale (e.g. right after a role is created), so always read fresh ones.
    fresh = sorted(await g.fetch_roles(), key=lambda r: r.position)
    role = find_role(g, a.get("role"), fresh)
    if role is None:
        return False, "Couldn't find that role."
    if role.is_default() or role.managed:
        return False, f"**{role.name}** can't be moved."
    above_ref, below_ref = a.get("above"), a.get("below")
    target = find_role(g, above_ref or below_ref, fresh)
    if not target or target.is_default():
        return False, "Couldn't find the role to place it next to."
    if target.id == role.id:
        return False, "That's the same role."

    me_ids = {r.id for r in g.me.roles}
    bot_top = max((r for r in fresh if r.id in me_ids), key=lambda r: r.position)
    if role.position >= bot_top.position:
        return False, f"**{role.name}** is above my role. Move my role higher first."
    privileged = ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id
    author_ids = {r.id for r in ctx.author.roles}
    author_top = max((r for r in fresh if r.id in author_ids), key=lambda r: r.position)
    if not privileged and role.position >= author_top.position:
        return False, f"**{role.name}** is above your highest role."

    word = "above" if above_ref else "below"
    if (above_ref and role.position > target.position) or (below_ref and role.position < target.position):
        return True, f"{role.mention} is already {word} {target.mention}"

    old_idx = next(i for i, r in enumerate(fresh) if r.id == role.id)
    order = [r for r in fresh if r.id != role.id]
    t_idx = next(i for i, r in enumerate(order) if r.id == target.id)
    order.insert(t_idx + 1 if above_ref else t_idx, role)
    new_idx = next(i for i, r in enumerate(order) if r.id == role.id)
    bot_idx = next(i for i, r in enumerate(order) if r.id == bot_top.id)
    if new_idx >= bot_idx:
        return False, "That spot is above my own role. Move my role higher first."
    if not privileged:
        auth_idx = next(i for i, r in enumerate(order) if r.id == author_top.id)
        if new_idx >= auth_idx:
            return False, "You can only place roles below your own highest role."

    lo, hi = sorted((old_idx, new_idx))
    changes = [{"id": order[i].id, "position": i} for i in range(lo, hi + 1) if order[i].position != i]
    if changes:
        route = Route("PATCH", "/guilds/{guild_id}/roles", guild_id=g.id)
        await bot.http.request(route, json=changes, reason=ctx.reason)

    # Don't trust the request: read the order back and confirm it really changed.
    after = {r.id: r.position for r in await g.fetch_roles()}
    ok = after.get(role.id, -1) > after.get(target.id, 0) if above_ref else after.get(role.id, 99999) < after.get(target.id, 0)
    if not ok:
        return False, "Discord didn't apply that move. Make sure my role is above both roles."
    return True, f"Moved {role.mention} {word} {target.mention}"

async def act_role_permissions(ctx, a):
    g = ctx.guild
    role = find_role(g, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    allow_raw, deny_raw = as_list(a.get("allow")), as_list(a.get("deny"))
    allow, deny = norm_perms(allow_raw), norm_perms(deny_raw)
    unknown = [str(x) for x in allow_raw + deny_raw if not norm_perm(x)]
    if not allow and not deny:
        return False, "I didn't recognise any of those permissions."
    if set(allow) & set(deny):
        return False, "Same permission in both allow and deny."
    is_owner = ctx.author.id in BOT_OWNER_IDS
    bad = [p for p in allow if p in DANGEROUS_PERMS]
    if bad and not is_owner:
        return False, "Only the bot owner can grant: " + ", ".join(p.replace("_", " ") for p in bad)
    mep = g.me.guild_permissions
    for p in allow:
        if not (mep.administrator or getattr(mep, p, False)):
            return False, f"I don't have **{p.replace('_', ' ')}** myself, so I can't grant it."
        if not (is_owner or ctx.author.id == g.owner_id):
            ap = ctx.author.guild_permissions
            if not (ap.administrator or getattr(ap, p, False)):
                return False, f"You don't have **{p.replace('_', ' ')}** yourself."
    perms = discord.Permissions(role.permissions.value)
    perms.update(**{**{p: True for p in allow}, **{p: False for p in deny}})
    await role.edit(permissions=perms, reason=ctx.reason)
    bits = []
    if allow:
        bits.append("allowed " + ", ".join(p.replace("_", " ") for p in allow))
    if deny:
        bits.append("denied " + ", ".join(p.replace("_", " ") for p in deny))
    text = f"Updated {role.mention}: " + "; ".join(bits)
    if unknown:
        text += f" (ignored: {', '.join(unknown)})"
    return True, text

ADMIN_ACTIONS = {
    "create_channel": act_create_channel, "delete_channel": act_delete_channel,
    "rename_channel": act_rename_channel, "channel_access": act_channel_access,
    "lock_channel": act_lock_channel, "unlock_channel": act_unlock_channel,
    "slowmode": act_slowmode, "set_topic": act_set_topic, "move_channel": act_move_channel,
    "create_role": act_create_role, "delete_role": act_delete_role, "rename_role": act_rename_role,
    "role_color": act_role_color, "give_role": act_give_role, "remove_role": act_remove_role,
    "move_role": act_move_role, "role_permissions": act_role_permissions,
    "role_style": act_role_style, "role_display": act_role_display,
    "kick": act_kick, "ban": act_ban, "unban": act_unban,
    "timeout": act_timeout, "untimeout": act_untimeout, "nickname": act_nickname,
    "purge": act_purge, "send_message": act_send_message,
}

# ---------- planning / confirmation / execution ----------
def needs_confirm(a):
    act = a.get("action")
    if act == "role_permissions" and any(p in DANGEROUS_PERMS for p in norm_perms(a.get("allow"))):
        return True
    return act in ALWAYS_CONFIRM or (act == "purge" and _to_int(a.get("count"), 10) > 20)

def summarize(a):
    label = str(a.get("action", "?")).replace("_", " ").capitalize()
    bits = []
    for k, v in a.items():
        if k == "action" or v in (None, "", []):
            continue
        bits.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    detail = ", ".join(bits)[:150]
    return f"**{label}**" + (f" ({detail})" if detail else "")

class ConfirmView(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value = None

    async def _gate(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your request.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, btn):
        if not await self._gate(interaction):
            return
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, btn):
        if not await self._gate(interaction):
            return
        self.value = False
        await interaction.response.defer()
        self.stop()

async def parse_admin_request(txt):
    """Returns list of actions ([] = not an admin request) or None if every AI provider failed."""
    messages = [{"role": "system", "content": ADMIN_PARSER_PROMPT}, {"role": "user", "content": txt}]
    raw, _, _ = await llm_complete(messages, groq_tokens=1500, or_tokens=800, temperature=0)
    if not raw:
        return None
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return []
    try:
        data = json.loads(raw[s:e + 1])
    except ValueError:
        return []
    acts = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(acts, list):
        return []
    return [a for a in acts if isinstance(a, dict) and isinstance(a.get("action"), str)][:6]

async def run_admin_flow(msg, txt):
    uid = msg.author.id
    try:
        async with msg.channel.typing():
            plan = await parse_admin_request(txt)

        if plan is None:
            await safe_reply(msg, "⚠️ Couldn't reach my brain right now, try again in a sec.")
            return

        if not plan:
            # Not actually an admin request: treat it as normal chat.
            user_cooldowns[uid] = time.time()
            await ai_queue.put((msg, txt, uid))
            return

        if any(needs_confirm(a) for a in plan):
            emb = discord.Embed(title="Confirm these actions",
                                description="\n".join(f"• {summarize(a)}" for a in plan), color=0xF1C40F)
            emb.set_footer(text="Confirm within 30 seconds")
            view = ConfirmView(uid)
            prompt = await safe_reply(msg, embed=emb, view=view)
            if prompt is None:
                return
            await view.wait()
            if not view.value:
                emb.title = "Cancelled" if view.value is False else "Timed out"
                emb.color = 0x95A5A6
                emb.set_footer(text="Nothing was changed")
                try:
                    await prompt.edit(embed=emb, view=None)
                except discord.HTTPException:
                    pass
                return
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                pass

        ctx = AdminCtx(msg)
        lines, ok_count = [], 0
        for a in plan:
            name = a["action"].strip().lower()
            handler = ADMIN_ACTIONS.get(name)
            if not handler:
                lines.append(f"❌ I can't do `{name}`.")
                continue
            perm = PERM_FOR.get(name)
            if perm and not has_perm(msg.author, perm):
                lines.append(f"❌ You need the **{perm.replace('_', ' ')}** permission for that.")
                continue
            try:
                ok, text = await handler(ctx, a)
            except discord.Forbidden:
                ok, text = False, "I'm missing permissions for that (check my role position)."
            except discord.HTTPException as e:
                print(f"admin action {name} failed:", e)
                ok, text = False, "Discord rejected that request."
            except Exception as e:
                print(f"admin action {name} crashed:", e)
                ok, text = False, "Something broke on that one."
            lines.append(f"{'✅' if ok else '❌'} {text}")
            ok_count += 1 if ok else 0

        if ok_count == len(plan):
            title, color = "Done", 0x2ECC71
        elif ok_count == 0:
            title, color = "Couldn't do that", 0xE74C3C
        else:
            title, color = "Partly done", 0xE67E22
        emb = discord.Embed(title=title, description="\n".join(lines)[:3900], color=color)
        emb.set_footer(text=f"Requested by {msg.author.display_name}")
        await safe_reply(msg, embed=emb)

        if ok_count:
            await log_action(msg.guild, "AI Admin Action",
                             f"<@{uid}> asked:\n> {txt[:300]}\n\n" + "\n".join(lines)[:1500])
    except Exception as e:
        print("run_admin_flow error:", e)
        await dm_owners(f"⚠️ **Admin flow exception:** `{e}`")
        await safe_reply(msg, "⚠️ Something went wrong, nothing else was changed.")

# ==========================================
# LLM HELPER + BACKGROUND QUEUE WORKER (GROQ -> OPENROUTER)
# ==========================================
GIF_RE = re.compile(r'https?://[^\s<>)]+?\.(?:gif|webp|png|jpg)(?![\w/])(?:\?[^\s<>)]*)?', re.IGNORECASE)

IMAGE_HOSTS = ("giphy.com", "tenor.com", "imgur.com", "discordapp.com", "discordapp.net")

async def verify_image_url(url):
    """Models invent GIF links. Only embed ones that are https, on a known host, and really return an image
    (a dead link is what shows up as an empty green bar)."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
            return False
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, allow_redirects=False) as r:
                return r.status == 200 and r.headers.get("Content-Type", "").lower().startswith("image/")
    except Exception:
        return False

def limit_words(t, n):
    """Hard word cap. Cuts at the last full sentence if there is one, else just trims."""
    words = t.split()
    if len(words) <= n:
        return t
    cut = " ".join(words[:n])
    ends = [cut.rfind(c) for c in ".!?"]
    last = max(ends)
    if last >= len(cut) // 2:
        return cut[:last + 1]
    return cut.rstrip(",;:- ")

def dedupe_sentences(t):
    """Models sometimes loop and repeat a sentence. Fix glued punctuation, then drop repeats."""
    t = re.sub(r"([?!])(?=[A-Za-z])", r"\1 ", t)
    parts = re.split(r"(?<=[.!?])\s+", t)
    seen, out = set(), []
    for p in parts:
        key = re.sub(r"[^a-z0-9]+", " ", p.lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(p)
    return " ".join(out)

def clean_ai_text(t):
    """Tidy model output: no stray headings, trailing spaces or big blank gaps."""
    t = t.replace("\r", "")
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u200d\ufe0f]", "", t)  # no emojis
    t = re.sub(r"\s*[\u2014\u2013]\s*", ", ", t)  # em/en dashes read as AI
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

async def llm_complete(messages, groq_tokens=1024, or_tokens=400, temperature=None):
    """Try Groq first, then OpenRouter free models. Returns (text_or_None, groq_err, openrouter_err).
    (gpt-oss models spend tokens on hidden reasoning, so groq_tokens must be generous.)"""
    extra = {} if temperature is None else {"temperature": temperature}
    ans, groq_err, openrouter_err = None, "", ""

    if groq_client:
        for groq_model in ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]:
            try:
                res = await groq_client.chat.completions.create(
                    model=groq_model, messages=messages, max_tokens=groq_tokens, **extra)
                if res.choices and (res.choices[0].message.content or "").strip():
                    return res.choices[0].message.content.strip(), groq_err, openrouter_err
            except Exception as e:
                groq_err = str(e)
                print(f"Groq ({groq_model}) error:", e)
    else:
        groq_err = "GROQ_API_KEY not set"

    if openrouter_client:
        for open_model in OPENROUTER_FREE_MODELS:
            try:
                res = await openrouter_client.chat.completions.create(
                    model=open_model, messages=messages, max_tokens=or_tokens,
                    extra_headers={"HTTP-Referer": "https://github.com", "X-Title": "Discord Bot"},
                    **extra)
                if res.choices and (res.choices[0].message.content or "").strip():
                    return res.choices[0].message.content.strip(), groq_err, openrouter_err
            except Exception as e:
                openrouter_err = str(e)
                print(f"OpenRouter ({open_model}) error:", e)
    else:
        openrouter_err = "OPENROUTER_API_KEY not set"

    return ans, groq_err, openrouter_err

async def process_ai_queue():
    while True:
        msg, txt, uid = await ai_queue.get()
        try:
            # 4-Second Artificial Typing Delay
            async with msg.channel.typing():
                await asyncio.sleep(4)

                if uid not in ai_history:
                    ai_history[uid] = []

                ai_history[uid].append({"role": "user", "content": txt})
                ai_history[uid] = ai_history[uid][-6:]

                messages = [{"role": "system", "content": pick_persona(msg.author)}] + ai_history[uid]
                ans, groq_err, openrouter_err = await llm_complete(messages, temperature=0.8)

                if not ans:
                    # Send detailed error logs directly to your Owner DM
                    await dm_owners(
                        f"⚠️ **AI Fallback Failed**\n• Groq Error: `{groq_err or 'None'}`\n"
                        f"• OpenRouter Error: `{openrouter_err or 'None'}`\n• User Message: `{txt[:300]}`")
                    # Send clean generic message in public channel
                    await safe_reply(msg, "⚠️ My brain laggin right now, try again in a sec bro.")
                    continue

                ans = limit_words(dedupe_sentences(clean_ai_text(ans)), MAX_WORDS)
                ai_history[uid].append({"role": "assistant", "content": ans})

                if len(ans) > 2000:
                    ans = ans[:1996] + "..."

                # Direct image/GIF links get embedded cleanly. (Giphy/Tenor page links are
                # left as-is: Discord unfurls those into GIFs by itself.)
                url_match = GIF_RE.search(ans)
                if url_match:
                    gif_url = url_match.group(0)
                    clean_text = ans.replace(gif_url, "").strip()
                    if await verify_image_url(gif_url):
                        emb = discord.Embed(color=0x2ECC71)
                        emb.set_image(url=gif_url)
                        if clean_text:
                            await safe_reply(msg, clean_text, embed=emb)
                        else:
                            await safe_reply(msg, embed=emb)
                    else:
                        # Dead/made-up link: drop it instead of posting an empty embed.
                        await safe_reply(msg, clean_text or "bruh")
                else:
                    await safe_reply(msg, ans)
        except Exception as e:
            print("AI Chat Error:", e)
            await dm_owners(f"⚠️ **Critical AI Exception:** `{e}`")
            await safe_reply(msg, "⚠️ Something went wrong, my bad bro.")
        finally:
            ai_queue.task_done()
            await asyncio.sleep(2)

# ==========================================
# ON MESSAGE EVENT (AI CHATBOT & GUILD CHECKS)
# ==========================================
_bg_tasks = set()

async def handle_ai_trigger(msg):
    global bot_muted

    # AI chat is disabled in small, non-exempt servers (commands still work there).
    if msg.guild and (msg.guild.member_count or 0) < 200 and msg.guild.id not in EXEMPT_GUILD_IDS:
        return

    # AI Chat Trigger (Ping or Reply)
    pinged = bot.user in msg.mentions
    ref = msg.reference.resolved if msg.reference else None
    is_reply = isinstance(ref, discord.Message) and ref.author == bot.user

    if not (pinged or is_reply):
        return

    txt = re.sub(rf"<@!?{bot.user.id}>", "", msg.content).strip()

    # Mute Command Check (Owner Only)
    if txt.lower() == "shut up":
        if msg.author.id in BOT_OWNER_IDS:
            bot_muted = True
        return

    if bot_muted:
        if txt.lower() == "you can talk again" and msg.author.id in BOT_OWNER_IDS:
            bot_muted = False
            await safe_reply(msg, "back, did you miss me?")
        return

    if not txt:
        await safe_reply(msg, "tf you want bro?")
        return

    uid = msg.author.id

    # Server-admin requests from staff skip the chat cooldown/queue and run on their own.
    # (Non-staff just get normal chat; the bot can't be talked into admin actions.)
    if msg.guild and is_staff(msg.author) and looks_like_admin_request(msg, txt):
        task = asyncio.create_task(run_admin_flow(msg, txt))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return

    now = time.time()

    # Individual per-user cooldown check (AI_COOLDOWN)
    last_msg_time = user_cooldowns.get(uid, 0)
    if now - last_msg_time < AI_COOLDOWN:
        await safe_reply(msg, f"ayo chill for {AI_COOLDOWN} seconds.", delete_after=5)
        return

    user_cooldowns[uid] = now

    q_size = ai_queue.qsize()
    await ai_queue.put((msg, txt, uid))

    if q_size > 0:
        await safe_reply(msg, f"⏳ Added to queue (Position #{q_size + 1}). Hold up...")

@bot.event
async def on_message(msg):
    if msg.author.bot:
        return

    if msg.guild and msg.guild.id in BLACKLISTED_GUILD_IDS:
        return

    try:
        await handle_ai_trigger(msg)
    except Exception as e:
        print("on_message AI error:", e)

    # Always reached now, so prefix commands (!sync) work regardless of AI state.
    await bot.process_commands(msg)

class RateModal(discord.ui.Modal, title="Rate Event"):
    rating = discord.ui.TextInput(label="Rating (1-5)", placeholder="e.g. 4.5", max_length=4)

    def __init__(self, host_id, key):
        super().__init__()
        self.host_id = str(host_id)
        self.key = key

    async def on_submit(self, interaction):
        info = db["events"].get(self.key)
        if not info:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        if time.time() - info.get("concluded_at", 0) > 3600:
            await interaction.response.send_message("Rating window closed.", ephemeral=True)
            return

        if interaction.user.id == info.get("host_id"):
            await interaction.response.send_message("You can't rate your own event.", ephemeral=True)
            return

        # Only players who actually joined can rate (events concluded before this
        # update have no participant list, so they stay open to everyone).
        participants = info.get("participants")
        if participants is not None and interaction.user.id not in participants:
            await interaction.response.send_message("Only players who joined this event can rate it.", ephemeral=True)
            return

        if "voted" not in info: 
            info["voted"] = []

        if interaction.user.id in info["voted"]:
            await interaction.response.send_message("You already voted.", ephemeral=True)
            return

        try:
            val = float(self.rating.value)
            if not (1.0 <= val <= 5.0): raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid rating.", ephemeral=True)
            return

        info["voted"].append(interaction.user.id)
        check_host(self.host_id)
        db["hosts"][self.host_id]["ratings"].append(val)
        save_db(db)

        await interaction.response.send_message(f"Rated **{val}/5**!", ephemeral=True)
        await update_leaderboard(interaction.guild)

class ClaimModal(discord.ui.Modal, title="Claim Spot"):
    country = discord.ui.TextInput(label="Nation", required=True, max_length=40)
    roblox = discord.ui.TextInput(label="Roblox Username", required=True, max_length=40)

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction):
        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        now = time.time()
        if now < info["start_time"]:
            await interaction.response.send_message("Not open yet.", ephemeral=True)
            return

        if now >= info["end_time"]:
            await interaction.response.send_message("Closed.", ephemeral=True)
            return

        for c_name, u in info["reservations"].items():
            if u["discord_id"] == interaction.user.id:
                await interaction.response.send_message("You already claimed a spot.", ephemeral=True)
                return

        c_title = self.country.value.title().strip()
        r_user = self.roblox.value.strip()

        if interaction.user.id in db["blacklists"].get(str(interaction.guild_id), []):
            await interaction.response.send_message("You are blacklisted.", ephemeral=True)
            return

        if "waitlist" not in info: 
            info["waitlist"] = []

        max_p = info.get("max_players", 0)

        if c_title in info["reservations"] or (max_p > 0 and len(info["reservations"]) >= max_p):
            for w in info["waitlist"]:
                if w["discord_id"] == interaction.user.id:
                    await interaction.response.send_message("Already on waitlist.", ephemeral=True)
                    return

            info["waitlist"].append({"discord_id": interaction.user.id, "roblox": r_user, "country": c_title})
            save_db(db)

            await refresh_event_message(info, interaction.guild)
            await interaction.response.send_message(f"Spot taken/full! Added to waitlist for {c_title}.", ephemeral=True)
            return

        matches = difflib.get_close_matches(c_title, list(info["reservations"].keys()), cutoff=0.88)
        if matches and matches[0].lower() != c_title.lower():
            await interaction.response.send_message(f"Name too similar to {matches[0]}.", ephemeral=True)
            return

        sps = [s.title() for s in info.get("superpowers", [])]
        if c_title in sps and not is_vip(interaction.user, interaction.guild_id):
            await interaction.response.send_message("That nation is a VIP Superpower.", ephemeral=True)
            return

        info["reservations"][c_title] = {"roblox": r_user, "discord_id": interaction.user.id}
        # They have a spot now, so any waitlist entry of theirs must go (no double spots).
        info["waitlist"] = [w for w in info["waitlist"] if w["discord_id"] != interaction.user.id]
        save_db(db)

        await add_event_role(interaction.guild, interaction.user.id)
        await refresh_event_message(info, interaction.guild)
        await log_action(interaction.guild, "Spot Claimed", f"<@{interaction.user.id}> reserved **{c_title}**.")
        await interaction.response.send_message(f"Reserved **{c_title}**!", ephemeral=True)

class SwapModal(discord.ui.Modal, title="Propose Swap"):
    target = discord.ui.TextInput(label="Nation You Want", required=True, max_length=40)

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction):
        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        t_c = self.target.value.title().strip()
        u_c = next((c for c, u in info["reservations"].items() if u["discord_id"] == interaction.user.id), None)

        if not u_c: 
            await interaction.response.send_message("You don't have a spot.", ephemeral=True)
            return

        if t_c not in info["reservations"]: 
            await interaction.response.send_message("No one has that spot.", ephemeral=True)
            return

        if t_c == u_c: 
            await interaction.response.send_message("Can't swap with yourself.", ephemeral=True)
            return

        sps = [s.title() for s in info.get("superpowers", [])]
        if t_c in sps and not is_vip(interaction.user, interaction.guild_id):
            await interaction.response.send_message("That nation is a VIP Superpower.", ephemeral=True)
            return

        t_id = info["reservations"][t_c]["discord_id"]
        ch_id = get_config(interaction.guild_id).get("swap_channel")

        if not ch_id: 
            await interaction.response.send_message("Swap channel not set up.", ephemeral=True)
            return

        swap_ch = interaction.guild.get_channel(ch_id)
        if not swap_ch: 
            await interaction.response.send_message("Swap channel missing.", ephemeral=True)
            return

        swap_cooldowns[interaction.user.id] = time.time()
        emb = discord.Embed(title="🔄 Swap Request", description=f"<@{interaction.user.id}> wants to swap with <@{t_id}>!\n\nGives: {get_flag(u_c)} **{u_c}**\nGets: {get_flag(t_c)} **{t_c}**", color=0xF1C40F)
        await swap_ch.send(content=f"<@{t_id}>", embed=emb, view=SwapAcceptView(self.key, interaction.user.id, t_id, u_c, t_c))
        await interaction.response.send_message(f"Proposal sent in <#{ch_id}>!", ephemeral=True)

class SwapAcceptView(discord.ui.View):
    def __init__(self, key, s_id, t_id, s_c, t_c):
        super().__init__(timeout=300)
        self.key = key
        self.s_id = s_id
        self.t_id = t_id
        self.s_c = s_c
        self.t_c = t_c

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction, btn):
        if interaction.user.id != self.t_id:
            await interaction.response.send_message("Not your swap offer.", ephemeral=True)
            return

        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        res = info["reservations"]

        if self.s_c not in res or self.t_c not in res or res[self.s_c]["discord_id"] != self.s_id or res[self.t_c]["discord_id"] != self.t_id:
            await interaction.response.send_message("Swap invalid now.", ephemeral=True)
            return

        # Re-check VIP rules for BOTH sides at accept time.
        sps = [s.title() for s in info.get("superpowers", [])]
        s_mem = interaction.guild.get_member(self.s_id)
        if (self.t_c in sps and not is_vip(s_mem, interaction.guild_id)) or \
           (self.s_c in sps and not is_vip(interaction.user, interaction.guild_id)):
            await interaction.response.send_message("Swap blocked: a VIP Superpower can only be held by VIPs.", ephemeral=True)
            return

        res[self.s_c], res[self.t_c] = res[self.t_c], res[self.s_c]
        save_db(db)

        for child in self.children: 
            child.disabled = True

        emb = interaction.message.embeds[0]
        emb.color = 0x2ECC71
        emb.title = "✅ Swap Completed"
        await interaction.response.edit_message(embed=emb, view=self)
        await refresh_event_message(info, interaction.guild)
        await log_action(interaction.guild, "Swap", f"<@{self.s_id}> and <@{self.t_id}> swapped spots.")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, btn):
        if interaction.user.id != self.t_id: 
            await interaction.response.send_message("Not your offer.", ephemeral=True)
            return

        for child in self.children: 
            child.disabled = True

        emb = interaction.message.embeds[0]
        emb.color = 0xE74C3C
        emb.title = "❌ Swap Declined"
        await interaction.response.edit_message(embed=emb, view=self)

class KickSelect(discord.ui.Select):
    def __init__(self, key, res):
        self.key = key
        opts = [discord.SelectOption(label=f"{get_flag(c)} {c}", value=c) for c in list(res.keys())[:25]]
        super().__init__(placeholder="Select player to kick...", options=opts)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        info = db["events"].get(self.key)
        c_title = self.values[0]

        if not info or c_title not in info["reservations"]:
            await interaction.followup.send("That spot is already gone.", ephemeral=True)
            return

        uid = info["reservations"][c_title]["discord_id"]
        del info["reservations"][c_title]

        await remove_event_role(interaction.guild, uid)

        ch = bot.get_channel(info["channel_id"])
        await process_waitlist(info, c_title, interaction.guild, ch)

        save_db(db)
        await refresh_event_message(info, interaction.guild)
        await log_action(interaction.guild, "Player Kicked", f"<@{interaction.user.id}> removed **{c_title}** (<@{uid}>).")
        await interaction.followup.send(f"Kicked **{c_title}**.", ephemeral=True)

class RateBtnView(discord.ui.View):
    def __init__(self, h_id, e_key):
        super().__init__(timeout=3600)
        self.h_id, self.e_key = h_id, e_key

    @discord.ui.button(label="Rate Host", style=discord.ButtonStyle.success)
    async def rate(self, i, b):
        await i.response.send_modal(RateModal(self.h_id, self.e_key))

class HostActionsView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=300)
        self.key = key
        info = db["events"].get(key)
        if info and info["reservations"]:
            self.add_item(KickSelect(key, info["reservations"]))

    async def interaction_check(self, interaction):
        if is_authorized_manager(interaction.user):
            return True
        await interaction.response.send_message("Admins or managers only.", ephemeral=True)
        return False

    @discord.ui.button(label="Clear Roles", style=discord.ButtonStyle.secondary, row=1)
    async def clear_roles(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        r_id = get_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(r_id) if r_id else None

        if not role:
            await interaction.followup.send("Event role not setup.", ephemeral=True)
            return

        count = 0
        for mem in list(role.members):
            try: 
                await mem.remove_roles(role)
                count += 1
            except discord.HTTPException: 
                pass

        await interaction.followup.send(f"Removed role from {count} members.", ephemeral=True)

    @discord.ui.button(label="Conclude Event", style=discord.ButtonStyle.danger, row=1)
    async def conclude(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        info = db["events"].get(self.key)

        if not info:
            await interaction.followup.send("Event not found.", ephemeral=True)
            return

        if info.get("concluded"): 
            await interaction.followup.send("Already ended.", ephemeral=True)
            return

        now = int(time.time())
        info["concluded"] = True
        info["concluded_at"] = now
        # Remember who played, so only they can rate the host.
        info["participants"] = [u["discord_id"] for u in info["reservations"].values()]

        r_id = get_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(r_id) if r_id else None
        if role:
            for mem in list(role.members):
                try: await mem.remove_roles(role)
                except discord.HTTPException: pass

        await refresh_event_message(info, interaction.guild, remove_view=True)

        # Time the event ran live after reservations closed (unchanged behaviour).
        dur = round(max(0, now - info["end_time"]) / 3600, 1)
        p_count = len(info["reservations"])
        hid = check_host(info["host_id"])

        db["hosts"][hid]["total_events"] += 1
        db["hosts"][hid]["total_players"] += p_count
        save_db(db)

        s_id = get_config(interaction.guild_id).get("stats_channel")
        if s_id:
            s_ch = interaction.guild.get_channel(s_id)
            if s_ch:
                emb = discord.Embed(title="📊 EVENT RESULTS", color=0xF1C40F)
                emb.add_field(name="Event", value=f"**{info['display_name']}**", inline=False)
                emb.add_field(name="Host", value=f"<@{info['host_id']}>", inline=True)
                emb.add_field(name="Duration", value=f"{dur} Hours", inline=True)
                emb.add_field(name="Players", value=f"{p_count}", inline=True)

                try:
                    await s_ch.send(embed=emb, view=RateBtnView(info["host_id"], self.key))
                except discord.HTTPException as e:
                    print("stats post failed:", e)

        await update_leaderboard(interaction.guild)
        await interaction.followup.send("Event concluded and stats posted.", ephemeral=True)

class EventButtonsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Spot", style=discord.ButtonStyle.success, custom_id="claim_btn")
    async def claim(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(ClaimModal(key))

    @discord.ui.button(label="Swap Nation", style=discord.ButtonStyle.primary, custom_id="swap_btn")
    async def swap(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return

        rem = 120 - (time.time() - swap_cooldowns.get(interaction.user.id, 0))
        if rem > 0:
            await interaction.response.send_message(f"Cooldown active. Wait {int(rem)}s.", ephemeral=True)
            return

        await interaction.response.send_modal(SwapModal(key))

    @discord.ui.button(label="Leave Event", style=discord.ButtonStyle.danger, custom_id="leave_btn")
    async def leave(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        key, info = find_event(interaction.message.id)
        if not key:
            await interaction.followup.send("This event no longer exists.", ephemeral=True)
            return

        tgt = next((c for c, u in info["reservations"].items() if u["discord_id"] == interaction.user.id), None)
        if tgt:
            del info["reservations"][tgt]
            await remove_event_role(interaction.guild, interaction.user.id)

            ch = bot.get_channel(info["channel_id"])
            await process_waitlist(info, tgt, interaction.guild, ch)

            save_db(db)
            await refresh_event_message(info, interaction.guild)
            await log_action(interaction.guild, "Spot Left", f"<@{interaction.user.id}> left {tgt}.")
            await interaction.followup.send("Spot removed.", ephemeral=True)
            return

        wait = info.get("waitlist", [])
        w_ent = next((w for w in wait if w["discord_id"] == interaction.user.id), None)
        if w_ent:
            wait.remove(w_ent)
            save_db(db)
            await refresh_event_message(info, interaction.guild)
            await interaction.followup.send("Removed from waitlist.", ephemeral=True)
            return

        await interaction.followup.send("You don't have a spot here.", ephemeral=True)

    @discord.ui.button(label="Host Actions", style=discord.ButtonStyle.secondary, custom_id="host_btn")
    async def host_menu(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        if not is_authorized_manager(interaction.user):
            await interaction.response.send_message("Admins or managers only.", ephemeral=True)
            return

        await interaction.response.send_message("Host Panel:", view=HostActionsView(key), ephemeral=True)

class SetupRolePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.RoleSelect(placeholder="Host Role", custom_id="r1"))
        self.add_item(discord.ui.RoleSelect(placeholder="Event Role", custom_id="r2"))
        self.add_item(discord.ui.RoleSelect(placeholder="VIP Role", custom_id="r3"))

    async def interaction_check(self, interaction):
        val = interaction.data["values"][0]
        cid = interaction.data["custom_id"]
        conf = get_config(interaction.guild_id)

        if cid == "r1": conf["host_role"] = int(val)
        elif cid == "r2": conf["event_role"] = int(val)
        elif cid == "r3": conf["superpower_role"] = int(val)

        save_db(db)
        await interaction.response.send_message("Role saved.", ephemeral=True)
        return True

class SetupChannelPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.ChannelSelect(placeholder="Log Channel", custom_id="c1"))
        self.add_item(discord.ui.ChannelSelect(placeholder="Swap Channel", custom_id="c2"))
        self.add_item(discord.ui.ChannelSelect(placeholder="Stats Channel", custom_id="c3"))

    async def interaction_check(self, interaction):
        val = interaction.data["values"][0]
        cid = interaction.data["custom_id"]
        conf = get_config(interaction.guild_id)

        if cid == "c1": conf["log_channel"] = int(val)
        elif cid == "c2": conf["swap_channel"] = int(val)
        elif cid == "c3": conf["stats_channel"] = int(val)

        save_db(db)
        await interaction.response.send_message("Channel saved.", ephemeral=True)
        return True

@bot.tree.command(name="clearchat", description="Clears your AI chat history")
async def clearchat_cmd(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in ai_history:
        del ai_history[uid]
        await interaction.response.send_message("AI memory cleared.", ephemeral=True)
    else:
        await interaction.response.send_message("No memory to clear.", ephemeral=True)

@bot.tree.command(name="clearchat_all", description="Owner only: wipes AI chat memory for everyone")
async def clearchat_all_cmd(interaction: discord.Interaction):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    n = len(ai_history)
    ai_history.clear()
    await interaction.response.send_message(f"Cleared AI memory for {n} user(s).", ephemeral=True)

@bot.tree.command(name="setup", description="Admin setup panel")
@app_commands.guild_only()
async def setup_cmd(interaction: discord.Interaction):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    conf = get_config(interaction.guild_id)
    e1 = discord.Embed(title="Setup Roles", description=f"Host: <@&{conf.get('host_role')}>\nEvent: <@&{conf.get('event_role')}>\nVIP: <@&{conf.get('superpower_role')}>")
    await interaction.response.send_message(embed=e1, view=SetupRolePanel(), ephemeral=True)

    e2 = discord.Embed(title="Setup Channels", description=f"Logs: <#{conf.get('log_channel')}>\nSwaps: <#{conf.get('swap_channel')}>\nStats: <#{conf.get('stats_channel')}>")
    await interaction.followup.send(embed=e2, view=SetupChannelPanel(), ephemeral=True)

# Last phase ("open"/"live") we rendered per event, so we only edit when it flips.
event_phase = {}

@tasks.loop(seconds=30)
async def refresh_timers():
    now = int(time.time())

    for key, info in list(db["events"].items()):
        try:
            if info.get("concluded"):
                event_phase.pop(key, None)
                continue

            phase = "live" if now >= info["end_time"] else "open"
            if event_phase.get(key) == phase:
                continue  # Countdown timestamps update client-side; nothing to edit.

            if await refresh_event_message(info):
                event_phase[key] = phase
        except Exception as e:
            print("refresh_timers error:", e)

    # Housekeeping so these dicts don't grow forever.
    cutoff = time.time() - 3600
    for d in (user_cooldowns, swap_cooldowns):
        for k in [k for k, v in d.items() if v < cutoff]:
            del d[k]
    while len(ai_history) > 500:
        ai_history.pop(next(iter(ai_history)))

@refresh_timers.before_loop
async def before_refresh_timers():
    await bot.wait_until_ready()

async def setup_hook():
    global ai_worker_task
    bot.add_view(EventButtonsView())
    refresh_timers.start()
    ai_worker_task = asyncio.create_task(process_ai_queue())

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    print(f"Logged in: {bot.user.name}")

@bot.command()
@commands.guild_only()
async def sync(ctx):
    if not is_server_admin(ctx.author):
        await ctx.send("Admins only.")
        return
    s = await bot.tree.sync()
    await ctx.send(f"Synced {len(s)} commands.")

async def ev_autocomplete(interaction: discord.Interaction, curr: str):
    ch = []
    for k, v in db["events"].items():
        if v.get("concluded") or not event_in_guild(v, interaction.guild_id):
            continue
        if curr.lower() in v["display_name"].lower():
            ch.append(app_commands.Choice(name=v["display_name"][:100], value=v["display_name"][:100]))
    return ch[:25]

@bot.tree.command(name="event", description="Create event banner")
@app_commands.guild_only()
async def create_ev(interaction: discord.Interaction, name: str, starts_in: str, duration: str, target: discord.TextChannel, max_p: int = 0):
    await interaction.response.defer(ephemeral=True)

    if not is_authorized_manager(interaction.user):
        await interaction.followup.send("Managers only.", ephemeral=True)
        return

    key = name.lower().strip()
    existing = db["events"].get(key)
    if existing and not existing.get("concluded"):
        await interaction.followup.send("An active event with that name already exists.", ephemeral=True)
        return

    s_off = parse_time(starts_in)
    d_sec = parse_time(duration)
    if d_sec <= 0: d_sec = 1800

    s_ts = int(time.time()) + s_off
    e_ts = s_ts + d_sec

    db["events"][key] = {
        "display_name": name, "start_time": s_ts, "end_time": e_ts,
        "host_id": interaction.user.id, "channel_id": target.id, "guild_id": interaction.guild_id,
        "message_ids": [], "superpowers": [], "reservations": {}, "waitlist": [],
        "voted": [], "max_players": max_p, "concluded": False
    }

    embs = build_embed(name, db["events"][key], interaction.guild)
    try:
        msg = await target.send(embeds=embs, view=EventButtonsView())
    except discord.HTTPException as e:
        del db["events"][key]
        print("event post failed:", e)
        await interaction.followup.send("I can't post in that channel (check my permissions).", ephemeral=True)
        return

    db["events"][key]["message_ids"] = [msg.id]
    save_db(db)

    await log_action(interaction.guild, "Event Created", f"**{name}** made by <@{interaction.user.id}>.")
    await interaction.followup.send("Event created.", ephemeral=True)

@bot.tree.command(name="set_superpowers", description="Set superpowers")
@app_commands.guild_only()
@app_commands.autocomplete(event_name=ev_autocomplete)
async def set_sp(interaction: discord.Interaction, event_name: str, nations: str):
    if not is_authorized_manager(interaction.user): 
        await interaction.response.send_message("Managers only.", ephemeral=True)
        return

    key = event_name.lower().strip()
    info = db["events"].get(key)
    if not info or not event_in_guild(info, interaction.guild_id): 
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    sps = [n.strip().title() for n in nations.split(",") if n.strip()]
    info["superpowers"] = sps
    save_db(db)

    await refresh_event_message(info, interaction.guild)
    await interaction.response.send_message(f"Superpowers set: {', '.join(sps)}", ephemeral=True)

@bot.tree.command(name="blacklist", description="Blacklist user")
@app_commands.guild_only()
async def blacklist(interaction: discord.Interaction, user: discord.Member):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    gid = str(interaction.guild_id)
    if gid not in db["blacklists"]: 
        db["blacklists"][gid] = []

    if user.id in db["blacklists"][gid]:
        db["blacklists"][gid].remove(user.id)
        msg = f"Unblacklisted {user.mention}."
    else:
        db["blacklists"][gid].append(user.id)
        msg = f"Blacklisted {user.mention}."

    save_db(db)
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="spawn_leaderboard", description="Spawn live leaderboard")
@app_commands.guild_only()
async def spawn_lb(interaction: discord.Interaction):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    emb = discord.Embed(title="Host Leaderboards", description="Updating...")
    msg = await interaction.channel.send(embed=emb)

    conf = get_config(interaction.guild_id)
    conf["leaderboard_msg_id"] = msg.id
    conf["leaderboard_ch_id"] = interaction.channel_id
    save_db(db)

    await update_leaderboard(interaction.guild)
    await interaction.response.send_message("Leaderboard spawned.", ephemeral=True)

@bot.tree.command(name="trust", description="Owner only: add/remove someone from the bot's admin list in THIS server")
@app_commands.guild_only()
async def trust_cmd(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    lst = db["trusted_users"].setdefault(str(interaction.guild_id), [])
    if user.id in lst:
        lst.remove(user.id)
        text = f"Removed {user.mention} from the admin list in this server."
    else:
        lst.append(user.id)
        text = f"Added {user.mention} to the admin list in this server."
    save_db(db)
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="trusted_list", description="Owner only: show who can use admin actions in this server")
@app_commands.guild_only()
async def trusted_list_cmd(interaction: discord.Interaction):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    here = db["trusted_users"].get(str(interaction.guild_id), [])
    fixed = list(dict.fromkeys(BOT_OWNER_IDS + ALLOWED_USER_IDS))
    text = ("**Everywhere (hardcoded):** " + (", ".join(f"<@{i}>" for i in fixed) or "none") +
            "\n**This server only:** " + (", ".join(f"<@{i}>" for i in here) or "none") +
            "\nServer admins can always use it too.")
    await interaction.response.send_message(text, ephemeral=True)

tk = os.environ.get("BOT_TOKEN")
if tk:
    keep_alive()
    bot.run(tk)
else:
    print("No token set!")
