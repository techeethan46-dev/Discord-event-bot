# Connected Discord-GitHub | Discord: AbyssalNoob1 | Roblox: AbyssalNoob1 

import discord
import os
import json
import time
import difflib
import asyncio
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
    app.run(host='0.0.0.0', port=os.environ.get("PORT", 10000))

def keep_alive():
    Thread(target=run_server, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# 100% FREE MULTI-PROVIDER AI CONFIGURATION
# (No Credit Cards / No Paid Accounts Required)
# ==========================================

# Primary: Groq API
groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

# Secondary Fallback: OpenRouter API (Strictly Free Models)
openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY")
)

OPENROUTER_FREE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "google/gemma-2-9b-it:free"
]

ai_history = {}
user_cooldowns = {}
ai_queue = asyncio.Queue()
bot_muted = False

BOT_OWNER_IDS = [
    1359263076838539296,  # Your User ID
]

BOT_PERSONA = """
You are a savage, funny Discord user with street attitude.
STRICT RULES:
1. Maximum 4 lines per reply. Keep it punchy.
2. Tone: Have a street attitude (use bro, tf, ain't no way, trippin, nah, fr), but keep it natural. You are not a try-hard gangster, just a chill guy who roasts people.
3. NEVER sound like an AI. Zero robotic politeness.
4. Banter aggressively, disagree, and call people out if they act weird or say dumb things. Don't hold back.
5. GIF Rule: Use a Tenor/Giphy GIF link in about 1 out of every 4 messages (25% of the time). Don't spam them constantly, but use them enough to be funny when reacting.
"""

EXEMPT_GUILD_IDS = [123456789012345678]
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
        except:
            pass

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)

    return {"server_config": {}, "events": {}, "blacklists": {}, "hosts": {}}

def save_db(data):
    if db_col is not None:
        try:
            db_col.replace_one(
                {"_id": "main"}, 
                {"_id": "main", "json_data": json.dumps(data)}, 
                upsert=True
            )
        except Exception as e:
            print("db save error:", e)

    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

db = load_db()
for k in ["server_config", "events", "blacklists", "hosts"]:
    if k not in db: 
        db[k] = {}

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

def is_admin_or_host(interaction):
    if interaction.user.guild_permissions.administrator: 
        return True
    role_id = get_config(interaction.guild_id).get("host_role")
    if role_id and role_id in [r.id for r in interaction.user.roles]:
        return True
    return False

async def log_action(guild, title, desc, color=0x3498DB):
    log_id = get_config(guild.id).get("log_channel")
    if not log_id: return
    channel = guild.get_channel(log_id)
    if channel:
        emb = discord.Embed(title=f"📝 {title}", description=desc, color=color)
        try: 
            await channel.send(embed=emb)
        except: 
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
    except: 
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

    await msg.edit(embed=emb)

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
    sp_role = get_config(guild.id).get("superpower_role")

    cands = []
    for w in list(wait):
        uid = w["discord_id"]
        c_name = w["country"].title()

        if uid in banned or c_name in res:
            if uid in banned: 
                wait.remove(w)
            continue

        if c_name in sp_list:
            mem = guild.get_member(uid)
            is_boost = mem.premium_since is not None if mem else False
            has_role = sp_role in [r.id for r in mem.roles] if (mem and sp_role) else False
            is_adm = mem.guild_permissions.administrator if mem else False
            if not (is_boost or has_role or is_adm): 
                continue

        cands.append(w)

    if not cands: 
        return

    promo = next((i for i in cands if i["country"].title() == opened.title()), cands[0])
    wait.remove(promo)

    info["reservations"][promo["country"].title()] = {"roblox": promo["roblox"], "discord_id": promo["discord_id"]}

    r_id = get_config(guild.id).get("event_role")
    if r_id:
        role = guild.get_role(r_id)
        mem = guild.get_member(promo["discord_id"])
        if role and mem:
            try: 
                await mem.add_roles(role)
            except: 
                pass

    if channel:
        try: 
            await channel.send(f"🔔 <@{promo['discord_id']}> **{promo['country'].title()}** opened up and you got it!")
        except: 
            pass

# ==========================================
# BACKGROUND QUEUE WORKER (GROQ -> OPENROUTER)
# ==========================================
async def process_ai_queue():
    while True:
        msg, txt, uid = await ai_queue.get()
        try:
            async with msg.channel.typing():
                if uid not in ai_history:
                    ai_history[uid] = []

                ai_history[uid].append({"role": "user", "content": txt})
                ai_history[uid] = ai_history[uid][-6:]

                messages = [{"role": "system", "content": BOT_PERSONA}] + ai_history[uid]

                ans = None
                groq_err = ""
                openrouter_err = ""

                # STEP 1: Try Groq API (Primary - openai/gpt-oss-20b)
                try:
                    res = await groq_client.chat.completions.create(
                        model="openai/gpt-oss-20b",
                        messages=messages,
                        max_tokens=250,
                    )
                    ans = res.choices[0].message.content
                except Exception as e:
                    groq_err = str(e)
                    print("Groq Error:", e)

                # STEP 2: Fall back to OpenRouter Free Models
                if not ans:
                    for model_name in OPENROUTER_FREE_MODELS:
                        try:
                            res = await openrouter_client.chat.completions.create(
                                model=model_name,
                                messages=messages,
                                max_tokens=250,
                                extra_headers={
                                    "HTTP-Referer": "https://github.com",
                                    "X-Title": "Discord Bot",
                                }
                            )
                            ans = res.choices[0].message.content
                            if ans:
                                break
                        except Exception as e:
                            openrouter_err = str(e)
                            print(f"OpenRouter ({model_name}) error:", e)
                            continue

                if not ans:
                    await msg.reply(f"⚠️ **AI Error:** Groq: `{groq_err or 'No response'}` | OpenRouter: `{openrouter_err or 'No response'}`")
                    continue

                ai_history[uid].append({"role": "assistant", "content": ans})

                if len(ans) > 2000:
                    ans = ans[:1996] + "..."

                await msg.reply(ans)
        except Exception as e:
            print("AI Chat Error:", e)
            await msg.reply(f"⚠️ **Error:** `{e}`")
        finally:
            ai_queue.task_done()
            await asyncio.sleep(2)

# ==========================================
# ON MESSAGE EVENT (AI CHATBOT & GUILD CHECKS)
# ==========================================
@bot.event
async def on_message(msg):
    global bot_muted

    if msg.author.bot:
        return

    # Server checks (Blacklist & 200 Member Limit)
    if msg.guild:
        if msg.guild.id in BLACKLISTED_GUILD_IDS:
            return

        if msg.guild.member_count < 200 and msg.guild.id not in EXEMPT_GUILD_IDS:
            return

    # AI Chat Trigger (Ping or Reply)
    pinged = bot.user in msg.mentions
    is_reply = False
    if msg.reference and msg.reference.resolved:
        if msg.reference.resolved.author == bot.user:
            is_reply = True

    if pinged or is_reply:
        txt = msg.content.replace(f"<@{bot.user.id}>", "").strip()

        # Mute Command Check (Owner Only - Silent Mute)
        if txt.lower() == "shut up":
            if msg.author.id in BOT_OWNER_IDS:
                bot_muted = True
            return

        # Muted state: Completely ignore everyone until Owner speaks
        if bot_muted:
            if txt.lower() == "you can talk again" and msg.author.id in BOT_OWNER_IDS:
                bot_muted = False
                await msg.reply("thank you father")
            return

        if not txt:
            await msg.reply("tf you want bro?")
        else:
            uid = msg.author.id
            now = time.time()

            # 7-second per-user cooldown check
            last_msg_time = user_cooldowns.get(uid, 0)
            if now - last_msg_time < 7:
                wait_sec = int(7 - (now - last_msg_time))
                await msg.reply(f"chill out bro, wait {wait_sec}s before messaging again.")
                return

            user_cooldowns[uid] = now

            # Enqueue message
            q_size = ai_queue.qsize()
            await ai_queue.put((msg, txt, uid))

            if q_size > 0:
                await msg.reply(f"⏳ Added to queue (Position #{q_size + 1}). Hold up...")

    await bot.process_commands(msg)

class RateModal(discord.ui.Modal, title="Rate Event"):
    rating = discord.ui.TextInput(label="Rating (1-5)", placeholder="e.g. 4.5", max_length=4)

    def __init__(self, host_id, key):
        super().__init__()
        self.host_id = str(host_id)
        self.key = key

    async def on_submit(self, interaction):
        info = db["events"].get(self.key, {})

        if time.time() - info.get("concluded_at", 0) > 3600:
            await interaction.response.send_message("Rating window closed.", ephemeral=True)
            return

        if "voted" not in info: 
            info["voted"] = []

        if interaction.user.id in info["voted"]:
            await interaction.response.send_message("You already voted.", ephemeral=True)
            return

        try:
            val = float(self.rating.value)
            if not (1.0 <= val <= 5.0): raise ValueError
        except:
            await interaction.response.send_message("Invalid rating.", ephemeral=True)
            return

        info["voted"].append(interaction.user.id)
        check_host(self.host_id)
        db["hosts"][self.host_id]["ratings"].append(val)
        save_db(db)

        await update_leaderboard(interaction.guild)
        await interaction.response.send_message(f"Rated **{val}/5**!", ephemeral=True)

class ClaimModal(discord.ui.Modal, title="Claim Spot"):
    country = discord.ui.TextInput(label="Nation", required=True)
    roblox = discord.ui.TextInput(label="Roblox Username", required=True)

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

            try:
                msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_ids"][0])
                await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
            except: 
                pass

            await interaction.response.send_message(f"Spot taken/full! Added to waitlist for {c_title}.", ephemeral=True)
            return

        matches = difflib.get_close_matches(c_title, list(info["reservations"].keys()), cutoff=0.88)
        if matches and matches[0].lower() != c_title.lower():
            await interaction.response.send_message(f"Name too similar to {matches[0]}.", ephemeral=True)
            return

        sps = [s.title() for s in info.get("superpowers", [])]
        if c_title in sps:
            sp_role = get_config(interaction.guild_id).get("superpower_role")
            has_role = sp_role in [r.id for r in interaction.user.roles] if sp_role else False
            if not (interaction.user.premium_since or has_role or interaction.user.guild_permissions.administrator):
                await interaction.response.send_message("That nation is a VIP Superpower.", ephemeral=True)
                return

        info["reservations"][c_title] = {"roblox": r_user, "discord_id": interaction.user.id}
        r_id = get_config(interaction.guild_id).get("event_role")
        if r_id:
            role = interaction.guild.get_role(r_id)
            if role:
                try: await interaction.user.add_roles(role)
                except: pass

        try:
            msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_ids"][0])
            await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
        except: pass

        save_db(db)
        await log_action(interaction.guild, "Spot Claimed", f"<@{interaction.user.id}> reserved **{c_title}**.")
        await interaction.response.send_message(f"Reserved **{c_title}**!", ephemeral=True)

class SwapModal(discord.ui.Modal, title="Propose Swap"):
    target = discord.ui.TextInput(label="Nation You Want", required=True)

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction):
        info = db["events"].get(self.key)
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
        res = info["reservations"]

        if self.s_c not in res or self.t_c not in res or res[self.s_c]["discord_id"] != self.s_id:
            await interaction.response.send_message("Swap invalid now.", ephemeral=True)
            return

        res[self.s_c], res[self.t_c] = res[self.t_c], res[self.s_c]

        try:
            msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_ids"][0])
            await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
        except: 
            pass

        save_db(db)
        for child in self.children: 
            child.disabled = True

        emb = interaction.message.embeds[0]
        emb.color = 0x2ECC71
        emb.title = "✅ Swap Completed"
        await interaction.response.edit_message(embed=emb, view=self)
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
        info = db["events"].get(self.key)
        c_title = self.values[0]
        uid = info["reservations"][c_title]["discord_id"]
        del info["reservations"][c_title]

        r_id = get_config(interaction.guild_id).get("event_role")
        if r_id:
            role = interaction.guild.get_role(r_id)
            if role:
                try: await interaction.guild.get_member(uid).remove_roles(role)
                except: pass

        ch = bot.get_channel(info["channel_id"])
        await process_waitlist(info, c_title, interaction.guild, ch)

        try:
            msg = await ch.fetch_message(info["message_ids"][0])
            await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
        except: 
            pass

        save_db(db)
        await interaction.response.send_message(f"Kicked **{c_title}**.", ephemeral=True)

class HostActionsView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=300)
        self.key = key
        info = db["events"].get(key)
        if info and info["reservations"]:
            self.add_item(KickSelect(key, info["reservations"]))

    @discord.ui.button(label="Clear Roles", style=discord.ButtonStyle.secondary, row=1)
    async def clear_roles(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        r_id = get_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(r_id) if r_id else None

        if not role:
            await interaction.followup.send("Event role not setup.", ephemeral=True)
            return

        count = 0
        for mem in role.members:
            try: 
                await mem.remove_roles(role)
                count += 1
            except: 
                pass

        await interaction.followup.send(f"Removed role from {count} members.", ephemeral=True)

    @discord.ui.button(label="Conclude Event", style=discord.ButtonStyle.danger, row=1)
    async def conclude(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        info = db["events"].get(self.key)

        if info.get("concluded"): 
            await interaction.followup.send("Already ended.", ephemeral=True)
            return

        now = int(time.time())
        info["concluded"] = True
        info["concluded_at"] = now

        r_id = get_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(r_id) if r_id else None
        if role:
            for mem in role.members:
                try: await mem.remove_roles(role)
                except: pass

        try:
            msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_ids"][0])
            await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild), view=None)
        except: pass

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

                class RateBtnView(discord.ui.View):
                    def __init__(self, h_id, e_key):
                        super().__init__(timeout=3600)
                        self.h_id, self.e_key = h_id, e_key
                    @discord.ui.button(label="Rate Host", style=discord.ButtonStyle.success)
                    async def rate(self, i, b):
                        await i.response.send_modal(RateModal(self.h_id, self.e_key))

                await s_ch.send(embed=emb, view=RateBtnView(info["host_id"], self.key))

        await update_leaderboard(interaction.guild)
        await interaction.followup.send("Event concluded and stats posted.", ephemeral=True)

class EventButtonsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Spot", style=discord.ButtonStyle.success, custom_id="claim_btn")
    async def claim(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if key: await interaction.response.send_modal(ClaimModal(key))

    @discord.ui.button(label="Swap Nation", style=discord.ButtonStyle.primary, custom_id="swap_btn")
    async def swap(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key: return

        rem = 120 - (time.time() - swap_cooldowns.get(interaction.user.id, 0))
        if rem > 0:
            await interaction.response.send_message(f"Cooldown active. Wait {int(rem)}s.", ephemeral=True)
            return

        await interaction.response.send_modal(SwapModal(key))

    @discord.ui.button(label="Leave Event", style=discord.ButtonStyle.danger, custom_id="leave_btn")
    async def leave(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        key, info = find_event(interaction.message.id)
        if not key: return

        tgt = next((c for c, u in info["reservations"].items() if u["discord_id"] == interaction.user.id), None)
        if tgt:
            del info["reservations"][tgt]
            r_id = get_config(interaction.guild_id).get("event_role")
            if r_id:
                try: await interaction.user.remove_roles(interaction.guild.get_role(r_id))
                except: pass

            ch = bot.get_channel(info["channel_id"])
            await process_waitlist(info, tgt, interaction.guild, ch)

            try:
                msg = await ch.fetch_message(info["message_ids"][0])
                await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
            except: pass

            save_db(db)
            await log_action(interaction.guild, "Spot Left", f"<@{interaction.user.id}> left {tgt}.")
            await interaction.followup.send("Spot removed.", ephemeral=True)
            return

        wait = info.get("waitlist", [])
        w_ent = next((w for w in wait if w["discord_id"] == interaction.user.id), None)
        if w_ent:
            wait.remove(w_ent)
            try:
                msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_ids"][0])
                await msg.edit(embeds=build_embed(info["display_name"], info, interaction.guild))
            except: pass
            save_db(db)
            await interaction.followup.send("Removed from waitlist.", ephemeral=True)
            return

        await interaction.followup.send("You don't have a spot here.", ephemeral=True)

    @discord.ui.button(label="Host Actions", style=discord.ButtonStyle.secondary, custom_id="host_btn")
    async def host_menu(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key: return
        if not is_admin_or_host(interaction):
            await interaction.response.send_message("Admins or hosts only.", ephemeral=True)
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

@bot.tree.command(name="setup", description="Admin setup panel")
async def setup_cmd(interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    conf = get_config(interaction.guild_id)
    e1 = discord.Embed(title="Setup Roles", description=f"Host: <@&{conf.get('host_role')}>\nEvent: <@&{conf.get('event_role')}>\nVIP: <@&{conf.get('superpower_role')}>")
    await interaction.response.send_message(embed=e1, view=SetupRolePanel(), ephemeral=True)

    e2 = discord.Embed(title="Setup Channels", description=f"Logs: <#{conf.get('log_channel')}>\nSwaps: <#{conf.get('swap_channel')}>\nStats: <#{conf.get('stats_channel')}>")
    await interaction.followup.send(embed=e2, view=SetupChannelPanel(), ephemeral=True)

@tasks.loop(seconds=30)
async def refresh_timers():
    for key, info in list(db["events"].items()):
        if info.get("concluded"): continue
        ch_id = info.get("channel_id")
        m_ids = info.get("message_ids", [])
        if not ch_id or not m_ids: continue
        ch = bot.get_channel(ch_id)
        if not ch: continue
        try:
            msg = await ch.fetch_message(m_ids[0])
            await msg.edit(embeds=build_embed(info["display_name"], info, ch.guild))
        except: 
            pass

async def setup_hook():
    bot.add_view(EventButtonsView())
    refresh_timers.start()
    asyncio.create_task(process_ai_queue())

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    print(f"Logged in: {bot.user.name}")

@bot.command()
async def sync(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admins only.")
        return
    s = await bot.tree.sync()
    await ctx.send(f"Synced {len(s)} commands.")

async def ev_autocomplete(interaction, curr):
    ch = []
    for k, v in db["events"].items():
        if not v.get("concluded") and curr.lower() in v["display_name"].lower():
            ch.append(app_commands.Choice(name=v["display_name"], value=v["display_name"]))
    return ch[:25]

@bot.tree.command(name="event", description="Create event banner")
async def create_ev(interaction, name: str, starts_in: str, duration: str, target: discord.TextChannel, max_p: int = 0):
    await interaction.response.defer(ephemeral=True)

    if not is_admin_or_host(interaction):
        await interaction.followup.send("Hosts only.", ephemeral=True)
        return

    s_off = parse_time(starts_in)
    d_sec = parse_time(duration)
    if d_sec == 0 and s_off == 0: d_sec = 1800

    s_ts = int(time.time()) + s_off
    e_ts = s_ts + d_sec
    key = name.lower().strip()

    db["events"][key] = {
        "display_name": name, "start_time": s_ts, "end_time": e_ts,
        "host_id": interaction.user.id, "channel_id": target.id,
        "message_ids": [], "superpowers": [], "reservations": {}, "waitlist": [],
        "voted": [], "max_players": max_p, "concluded": False
    }

    embs = build_embed(name, db["events"][key], interaction.guild)
    msg = await target.send(embeds=embs, view=EventButtonsView())
    db["events"][key]["message_ids"] = [msg.id]
    save_db(db)

    await log_action(interaction.guild, "Event Created", f"**{name}** made by <@{interaction.user.id}>.")
    await interaction.followup.send("Event created.", ephemeral=True)

@bot.tree.command(name="set_superpowers", description="Set superpowers")
@app_commands.autocomplete(event_name=ev_autocomplete)
async def set_sp(interaction, event_name: str, nations: str):
    if not is_admin_or_host(interaction): 
        await interaction.response.send_message("Hosts only.", ephemeral=True)
        return

    key = event_name.lower().strip()
    if key not in db["events"]: 
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    sps = [n.strip().title() for n in nations.split(",") if n.strip()]
    db["events"][key]["superpowers"] = sps

    try:
        msg = await bot.get_channel(db["events"][key]["channel_id"]).fetch_message(db["events"][key]["message_ids"][0])
        await msg.edit(embeds=build_embed(db["events"][key]["display_name"], db["events"][key], interaction.guild))
    except: 
        pass

    save_db(db)
    await interaction.response.send_message(f"Superpowers set: {', '.join(sps)}", ephemeral=True)

@bot.tree.command(name="blacklist", description="Blacklist user")
async def blacklist(interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
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
async def spawn_lb(interaction):
    if not interaction.user.guild_permissions.administrator:
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

tk = os.environ.get("BOT_TOKEN")
if tk:
    keep_alive()
    bot.run(tk)
else:
    print("No token set!")
