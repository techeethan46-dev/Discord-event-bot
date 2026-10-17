import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import time
import difflib
import pymongo
import asyncio
from flask import Flask
from threading import Thread

# Flask Web Server to keep Render active 24/7
app = Flask('')

@app.route('/')
def home(): 
    return "Main Bot is Live!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "events_data.json"

# ---------------------------------------------------------
# MONGODB & DATA PERSISTENCE
# ---------------------------------------------------------

MONGO_URI = os.environ.get("MONGO_URI")
mongo_client = pymongo.MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["ctw_bot_db"] if mongo_client else None
collection = mongo_db["bot_data"] if mongo_db is not None else None

def sanitize_keys_for_mongo(d):
    """Recursively replaces '.' in dictionary keys with '•' for MongoDB compatibility."""
    if isinstance(d, dict):
        new_d = {}
        for k, v in d.items():
            new_k = k.replace(".", "•")
            new_d[new_k] = sanitize_keys_for_mongo(v)
        return new_d
    elif isinstance(d, list):
        return [sanitize_keys_for_mongo(i) for i in d]
    return d

def desanitize_keys_from_mongo(d):
    """Recursively restores '.' in dictionary keys when loaded from MongoDB."""
    if isinstance(d, dict):
        new_d = {}
        for k, v in d.items():
            new_k = k.replace("•", ".")
            new_d[new_k] = desanitize_keys_from_mongo(v)
        return new_d
    elif isinstance(d, list):
        return [desanitize_keys_from_mongo(i) for i in d]
    return d

def load_data():
    if collection is not None:
        try:
            doc = collection.find_one({"_id": "main_data"})
            if doc and "content" in doc:
                return desanitize_keys_from_mongo(doc["content"])
        except Exception as e:
            print(f"Error loading from MongoDB: {e}")
    
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"server_config": {}, "events": {}, "blacklists": {}, "hosts": {}}

def _save_data_sync(data):
    if collection is not None:
        try:
            sanitized = sanitize_keys_for_mongo(data)
            collection.replace_one(
                {"_id": "main_data"},
                {"_id": "main_data", "content": sanitized},
                upsert=True
            )
            return
        except Exception as e:
            print(f"Error saving to MongoDB: {e}")
    
    temp_file = f"{DATA_FILE}.tmp"
    with open(temp_file, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(temp_file, DATA_FILE)

def save_data(data):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_save_data_sync, data))
    except RuntimeError:
        _save_data_sync(data)

db = load_data()
if "server_config" not in db: db["server_config"] = {}
if "events" not in db: db["events"] = {}
if "blacklists" not in db: db["blacklists"] = {}
if "hosts" not in db: db["hosts"] = {}

swap_cooldowns = {}

# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------

def get_server_config(guild_id) -> dict:
    gid = str(guild_id)
    if gid not in db["server_config"] or not isinstance(db["server_config"][gid], dict):
        db["server_config"][gid] = {}
        save_data(db)
    return db["server_config"][gid]

def init_host(host_id):
    hid = str(host_id)
    if hid not in db["hosts"]:
        db["hosts"][hid] = {"total_events": 0, "total_players": 0, "ratings": []}
    return hid

def parse_duration(time_str: str) -> int:
    time_str = time_str.lower().replace(" ", "")
    total_seconds = 0
    current_num = ""
    for char in time_str:
        if char.isdigit():
            current_num += char
        elif char in ['h', 'm', 's'] and current_num:
            if char == 'h': total_seconds += int(current_num) * 3600
            elif char == 'm': total_seconds += int(current_num) * 60
            elif char == 's': total_seconds += int(current_num)
            current_num = ""
    return total_seconds

FLAG_MAP = {
    "usa": "🇺🇸", "united states": "🇺🇸", "america": "🇺🇸",
    "russia": "🇷🇺", "ukraine": "🇺🇦", "france": "🇫🇷", "britain": "🇬🇧",
    "uk": "🇬🇧", "united kingdom": "🇬🇧", "germany": "🇩🇪", "china": "🇨🇳",
    "japan": "🇯🇵", "italy": "🇮🇹", "canada": "🇨🇦", "spain": "🇪🇸",
    "portugal": "🇵🇹", "poland": "🇵🇱", "turkey": "🇹🇷", "greece": "🇬🇷",
    "brazil": "🇧🇷", "india": "🇮🇳", "australia": "🇦🇺", "sweden": "🇸🇪"
}

def get_flag(country_name):
    return FLAG_MAP.get(country_name.lower().strip(), "🏴")

def get_event_by_message_id(msg_id):
    for event_key, event_info in db["events"].items():
        if msg_id in event_info.get("message_ids", []):
            return event_key, event_info
    return None, None

def is_host_or_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator: return True
    config = get_server_config(interaction.guild_id)
    host_role_id = config.get("host_role")
    if host_role_id and host_role_id in [r.id for r in interaction.user.roles]: return True
    return False

async def send_audit_log(guild, title, description, color=0x3498DB):
    config = get_server_config(guild.id)
    log_channel_id = config.get("log_channel")
    if log_channel_id:
        channel = guild.get_channel(log_channel_id)
        if channel:
            embed = discord.Embed(title=f"📝 {title}", description=description, color=color)
            embed.set_footer(text=f"Host Assistant Logs • {time.strftime('%H:%M:%S')}")
            try: await channel.send(embed=embed)
            except Exception: pass

async def update_live_leaderboard(guild: discord.Guild):
    config = get_server_config(guild.id)
    lb_msg_id = config.get("leaderboard_msg_id")
    lb_ch_id = config.get("leaderboard_ch_id")
    if not lb_msg_id or not lb_ch_id: return
    
    channel = guild.get_channel(lb_ch_id)
    if not channel: return
    try: msg = await channel.fetch_message(lb_msg_id)
    except Exception: return

    host_scores = []
    for hid, data in db["hosts"].items():
        avg = sum(data["ratings"])/len(data["ratings"]) if data["ratings"] else 0.0
        host_scores.append((hid, data["total_events"], data["total_players"], round(avg, 1)))

    embed = discord.Embed(title="🏆 Live Host Leaderboards", description=f"Last updated: <t:{int(time.time())}:R>", color=0xF1C40F)
    top_events = sorted(host_scores, key=lambda x: x[1], reverse=True)[:3]
    embed.add_field(name="🔥 Most Events Hosted", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[1]} Events" for i, h in enumerate(top_events) if h[1] > 0]) or "None", inline=False)
    
    top_players = sorted(host_scores, key=lambda x: x[2], reverse=True)[:3]
    embed.add_field(name="👥 Most Players Hosted", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[2]} Players" for i, h in enumerate(top_players) if h[2] > 0]) or "None", inline=False)
    
    top_rated = sorted([h for h in host_scores if len(db["hosts"][h[0]]["ratings"]) >= 2], key=lambda x: x[3], reverse=True)[:3]
    embed.add_field(name="⭐ Highest Rated (Min 2 votes)", value="\n".join([f"**{i+1}.** <@{h[0]}> - {h[3]} ⭐" for i, h in enumerate(top_rated)]) or "Not enough data", inline=False)

    await msg.edit(embed=embed)

def build_event_embeds(event_name, event_info, guild=None):
    start_res_ts = event_info["start_time"]
    end_res_ts = event_info["end_time"]
    host_id = event_info["host_id"]
    cohost_id = event_info.get("cohost_id")
    reservations = event_info["reservations"]
    waitlist = event_info.get("waitlist", [])
    sp_list = event_info.get("superpowers", [])
    max_players = event_info.get("max_players", 0)
    is_concluded = event_info.get("concluded", False)
    
    now = int(time.time())
    is_live = now >= end_res_ts and not is_concluded

    sp_role_id = get_server_config(guild.id).get("superpower_role") if guild else None
    sp_lines, reg_lines = [], []

    for country, u_data in reservations.items():
        u_id = u_data['discord_id']
        badge = ""
        if guild:
            member = guild.get_member(u_id)
            if member and (member.premium_since or (sp_role_id and sp_role_id in [r.id for r in member.roles])):
                badge = " 💎"
        line = f"• {get_flag(country)} **{country}:** `{u_data['roblox']}` (<@{u_id}>){badge}"
        if country.title() in [s.title() for s in sp_list]: sp_lines.append(line)
        else: reg_lines.append(line)

    sp_section = ""
    if sp_list:
        unclaimed = [s for s in sp_list if s.title() not in reservations]
        text = "\n".join(sp_lines) if sp_lines else ""
        if unclaimed: text += f"\n*Available:* " + ", ".join([f"**{s}**" for s in unclaimed])
        sp_section = f"### ⭐ Superpowers\n{text.strip()}\n\n"

    waitlist_section = ""
    if waitlist:
        w_lines = [f"• {get_flag(w['country'])} **{w['country']}**: <@{w['discord_id']}>" for w in waitlist[:5]]
        waitlist_section = f"\n\n### ⏳ Waitlist ({len(waitlist)})\n" + "\n".join(w_lines)

    if is_concluded:
        status_header = "🔒 **[CONCLUDED]**"
        time_status = f"• **Reservations Ended:** <t:{end_res_ts}:f>"
        embed_color = 0xE74C3C # Red
    elif is_live:
        status_header = "⚔️ **[EVENT LIVE]**"
        time_status = f"• **Reservations Closed:** <t:{end_res_ts}:f>\n• **Event Elapsed:** <t:{end_res_ts}:R>"
        embed_color = 0xF1C40F # Gold
    else:
        status_header = "🚩 **[RESERVATIONS OPEN]**"
        time_status = f"• **Reservations Start:** <t:{start_res_ts}:f>\n• **Reservations End:** <t:{end_res_ts}:f> (<t:{end_res_ts}:R>)"
        embed_color = 0x2ECC71 # Green

    embed = discord.Embed(color=embed_color)
    embed.title = f"{status_header} {event_name}"
    
    host_str = f"<@{host_id}>"
    if cohost_id: host_str += f" & <@{cohost_id}>"

    desc_content = (f"### 🗓️ Reservation Schedule\n{time_status}\n• **Hosted by:** {host_str}\n\n─────────────\n\n"
                    f"{sp_section}### 📋 Current Reservations\n" + ("\n".join(reg_lines) if reg_lines else "*None set*") + waitlist_section)
    
    if len(desc_content) > 3900:
        desc_content = desc_content[:3850] + "\n\n*... list truncated due to length limit.*"

    embed.description = desc_content
    embed.set_footer(text=f"Total Joined: {len(reservations)}{f' / {max_players}' if max_players > 0 else ''} Players  •  OBBY IS GOAT")
    return [embed]

async def process_waitlist_for_opened_spot(event_info, opened_country, guild, channel):
    waitlist = event_info.get("waitlist", [])
    if not waitlist: return

    res = event_info.get("reservations", {})
    max_p = event_info.get("max_players", 0)
    if max_p > 0 and len(res) >= max_p: return

    blacklist = db["blacklists"].get(str(guild.id), [])
    sp_list = [s.title() for s in event_info.get("superpowers", [])]
    sp_role_id = get_server_config(guild.id).get("superpower_role")

    candidates = []
    for item in list(waitlist):
        u_id = item["discord_id"]
        c_title = item["country"].title()

        if u_id in blacklist:
            waitlist.remove(item)
            continue

        if c_title in res:
            continue

        if c_title in sp_list:
            member = guild.get_member(u_id)
            is_booster = member.premium_since is not None if member else False
            has_sp_role = sp_role_id in [r.id for r in member.roles] if (member and sp_role_id) else False
            is_admin = member.guild_permissions.administrator if member else False
            if not (is_booster or has_sp_role or is_admin):
                continue

        candidates.append(item)

    if not candidates: return

    promoted_user = next((i for i in candidates if i["country"].title() == opened_country.title()), candidates[0])

    waitlist.remove(promoted_user)
    c_title = promoted_user["country"].title()
    u_id = promoted_user["discord_id"]
    r_user = promoted_user["roblox"]

    event_info["reservations"][c_title] = {"roblox": r_user, "discord_id": u_id}

    role_id = get_server_config(guild.id).get("event_role")
    if role_id:
        role = guild.get_role(role_id)
        member = guild.get_member(u_id)
        if role and member:
            try: await member.add_roles(role)
            except Exception: pass

    if channel:
        try:
            await channel.send(f"🔔 <@{u_id}> **{c_title}** opened up and you automatically claimed it from the waitlist!")
        except Exception: pass

# ---------------------------------------------------------
# EVENT MODALS & VIEWS
# ---------------------------------------------------------

class RateModal(discord.ui.Modal, title="Rate This Event"):
    rating = discord.ui.TextInput(label="Rating (1 to 5)", placeholder="e.g. 4.5, 5, 3.2", max_length=4)

    def __init__(self, host_id, event_key):
        super().__init__()
        self.host_id = str(host_id)
        self.event_key = event_key

    async def on_submit(self, interaction: discord.Interaction):
        event_info = db["events"].get(self.event_key, {})
        concluded_at = event_info.get("concluded_at", 0)

        if time.time() - concluded_at > 3600:
            await interaction.response.send_message("❌ The 1-hour rating window for this event has expired! Contact a server host if you need help.", ephemeral=True)
            return

        if "voted_users" not in event_info: event_info["voted_users"] = []
        if interaction.user.id in event_info["voted_users"]:
            await interaction.response.send_message("❌ You have already submitted a rating for this event!", ephemeral=True)
            return

        try:
            val = float(self.rating.value)
            if val < 1.0 or val > 5.0: raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Invalid rating! Please enter a number between 1.0 and 5.0.", ephemeral=True)
            return

        event_info["voted_users"].append(interaction.user.id)
        init_host(self.host_id)
        db["hosts"][self.host_id]["ratings"].append(val)
        save_data(db)

        await update_live_leaderboard(interaction.guild)
        await interaction.response.send_message(f"⭐ Thank you! You rated the event **{val}/5**.", ephemeral=True)

class ClaimModal(discord.ui.Modal, title="Claim Reservation"):
    country = discord.ui.TextInput(label="Nation", required=True)
    roblox = discord.ui.TextInput(label="Roblox Username", required=True)

    def __init__(self, event_key):
        super().__init__()
        self.event_key = event_key

    async def on_submit(self, interaction: discord.Interaction):
        event_info = db["events"].get(self.event_key)
        if not event_info or event_info.get("concluded", False):
            await interaction.response.send_message("❌ This event has concluded! Please contact a server host for details on upcoming events.", ephemeral=True)
            return

        now = time.time()
        if now < event_info["start_time"]:
            await interaction.response.send_message(f"🔒 Reservations haven't opened yet! They open <t:{event_info['start_time']}:R>. Please contact a server host if you have questions.", ephemeral=True)
            return
        if now >= event_info["end_time"]:
            await interaction.response.send_message("🔒 Reservations are closed! The event is already live in-game. Please contact a server host if you need assistance.", ephemeral=True)
            return

        for country_name, u_data in event_info["reservations"].items():
            if u_data["discord_id"] == interaction.user.id:
                await interaction.response.send_message(f"🚫 You already hold a reservation for **{country_name}**! Leave your current spot or use Swap Nation if you want a different country.", ephemeral=True)
                return

        c_title = self.country.value.title().strip()
        r_user = self.roblox.value.strip()

        if interaction.user.id in db["blacklists"].get(str(interaction.guild_id), []):
            await interaction.response.send_message("🚫 You are blacklisted from claiming event spots! Please contact a server administrator to appeal.", ephemeral=True)
            return

        if "waitlist" not in event_info: event_info["waitlist"] = []

        max_p = event_info.get("max_players", 0)
        is_full = max_p > 0 and len(event_info["reservations"]) >= max_p
        is_taken = c_title in event_info["reservations"]

        if is_taken or is_full:
            for w in event_info["waitlist"]:
                if w["discord_id"] == interaction.user.id:
                    await interaction.response.send_message("🚫 You are already on the waitlist for this event! Please wait for a spot to open.", ephemeral=True)
                    return

            event_info["waitlist"].append({"discord_id": interaction.user.id, "roblox": r_user, "country": c_title})
            save_data(db)

            try:
                msg = await bot.get_channel(event_info["channel_id"]).fetch_message(event_info["message_ids"][0])
                await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
            except Exception: pass

            await interaction.response.send_message(f"⏳ **{c_title}** is currently taken or full! You were added to the waitlist. Contact a server host if you need help.", ephemeral=True)
            return

        matches = difflib.get_close_matches(c_title, list(event_info["reservations"].keys()), cutoff=0.88)
        if matches and matches[0].lower() != c_title.lower():
            await interaction.response.send_message(f"🚫 Nation name **{c_title}** is too similar to already reserved nation **{matches[0]}**! Please pick a distinct name or contact a server host.", ephemeral=True)
            return

        superpowers_list = [s.title() for s in event_info.get("superpowers", [])]
        if c_title in superpowers_list:
            config = get_server_config(interaction.guild_id)
            sp_role_id = config.get("superpower_role")
            is_booster = interaction.user.premium_since is not None
            has_sp_role = sp_role_id in [r.id for r in interaction.user.roles] if sp_role_id else False
            is_admin = interaction.user.guild_permissions.administrator
            if not (is_booster or has_sp_role or is_admin):
                await interaction.response.send_message(f"💎 **{c_title}** is a Superpower! Reserved exclusively for Server Boosters & VIPs. Contact a server administrator or host if you believe this is an error.", ephemeral=True)
                return

        event_info["reservations"][c_title] = {"roblox": r_user, "discord_id": interaction.user.id}
        role_id = get_server_config(interaction.guild_id).get("event_role")
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try: await interaction.user.add_roles(role)
                except Exception: pass

        try:
            msg = await bot.get_channel(event_info["channel_id"]).fetch_message(event_info["message_ids"][0])
            await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
        except Exception: pass
        
        save_data(db)
        await send_audit_log(interaction.guild, "Reservation Claimed", f"<@{interaction.user.id}> reserved **{c_title}**.")
        await interaction.response.send_message(f"🚀 Reserved **{c_title}**!", ephemeral=True)

class SwapModal(discord.ui.Modal, title="Propose Nation Swap"):
    target_nation = discord.ui.TextInput(label="Nation You Want", required=True)

    def __init__(self, event_key):
        super().__init__()
        self.event_key = event_key

    async def on_submit(self, interaction: discord.Interaction):
        event_info = db["events"].get(self.event_key)
        target_c = self.target_nation.value.title().strip()
        user_c = next((c for c, u in event_info["reservations"].items() if u["discord_id"] == interaction.user.id), None)

        if not user_c:
            await interaction.response.send_message("❌ You do not hold a nation reservation in this event to swap! Claim a spot first or contact a server host.", ephemeral=True)
            return
        if target_c not in event_info["reservations"]:
            await interaction.response.send_message(f"❌ **{target_c}** is not reserved by anyone! Claim it directly instead or contact a server host.", ephemeral=True)
            return
        if target_c == user_c:
            await interaction.response.send_message("❌ Cannot swap a nation with yourself!", ephemeral=True)
            return

        target_user_id = event_info["reservations"][target_c]["discord_id"]
        swap_channel_id = get_server_config(interaction.guild_id).get("swap_channel")
        if not swap_channel_id:
            await interaction.response.send_message("❌ Swap Requests channel has not been configured by admins yet! Please contact a server administrator to configure it using /setup.", ephemeral=True)
            return
            
        swap_channel = interaction.guild.get_channel(swap_channel_id)
        if not swap_channel:
            await interaction.response.send_message("❌ The configured Swap channel no longer exists! Please contact a server administrator.", ephemeral=True)
            return

        swap_cooldowns[interaction.user.id] = time.time()
        embed = discord.Embed(title="🔄 Nation Swap Proposal", description=f"<@{interaction.user.id}> wants to swap with <@{target_user_id}>!\n\n• Gives: {get_flag(user_c)} **{user_c}**\n• Gets: {get_flag(target_c)} **{target_c}**", color=0xF1C40F)
        await swap_channel.send(content=f"<@{target_user_id}>", embed=embed, view=SwapAcceptView(self.event_key, interaction.user.id, target_user_id, user_c, target_c))
        await interaction.response.send_message(f"📨 Swap proposal sent to <#{swap_channel_id}>!", ephemeral=True)

class SwapAcceptView(discord.ui.View):
    def __init__(self, event_key, sender_id, target_id, sender_c, target_c):
        super().__init__(timeout=300)
        self.event_key, self.sender_id, self.target_id, self.sender_c, self.target_c = event_key, sender_id, target_id, sender_c, target_c

    @discord.ui.button(label="Accept Swap", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("❌ Only the targeted user can accept this swap offer!", ephemeral=True)
            return
        event_info = db["events"].get(self.event_key)
        res = event_info["reservations"]

        if self.sender_c not in res or self.target_c not in res or res[self.sender_c]["discord_id"] != self.sender_id:
            await interaction.response.send_message("❌ Swap invalid! One of the nations is no longer held by the original owners. Contact a server host if you need help.", ephemeral=True)
            return

        res[self.sender_c], res[self.target_c] = res[self.target_c], res[self.sender_c]

        try:
            msg = await bot.get_channel(event_info["channel_id"]).fetch_message(event_info["message_ids"][0])
            await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
        except Exception: pass
        save_data(db)

        for child in self.children: child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = 0x2ECC71
        embed.title = "✅ Nation Swap Completed"
        await interaction.response.edit_message(embed=embed, view=self)
        await send_audit_log(interaction.guild, "Nation Swap", f"<@{self.sender_id}> and <@{self.target_id}> swapped **{self.sender_c}** and **{self.target_c}**.")

    @discord.ui.button(label="Decline Swap", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("❌ Only the targeted user can decline this swap offer!", ephemeral=True)
            return
        for child in self.children: child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = 0xE74C3C
        embed.title = "❌ Nation Swap Declined"
        await interaction.response.edit_message(embed=embed, view=self)

# ---------------------------------------------------------
# HOST PANEL & VIEWS
# ---------------------------------------------------------

class KickSelect(discord.ui.Select):
    def __init__(self, event_key, reservations):
        self.event_key = event_key
        opts = [discord.SelectOption(label=f"{get_flag(c)} {c}", value=c) for c in list(reservations.keys())[:25]]
        super().__init__(placeholder="Select a player to kick...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        event_info = db["events"].get(self.event_key)
        c_title = self.values[0]
        uid = event_info["reservations"][c_title]["discord_id"]
        del event_info["reservations"][c_title]

        role_id = get_server_config(interaction.guild_id).get("event_role")
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try: await interaction.guild.get_member(uid).remove_roles(role)
                except Exception: pass

        target_channel = bot.get_channel(event_info["channel_id"])
        await process_waitlist_for_opened_spot(event_info, c_title, interaction.guild, target_channel)

        try:
            msg = await target_channel.fetch_message(event_info["message_ids"][0])
            await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
        except Exception: pass
        
        save_data(db)
        await interaction.response.send_message(f"⚡ Kicked **{c_title}**!", ephemeral=True)

class HostActionsView(discord.ui.View):
    def __init__(self, event_key):
        super().__init__(timeout=300)
        self.event_key = event_key
        event_info = db["events"].get(event_key)
        if event_info and event_info["reservations"]:
            self.add_item(KickSelect(event_key, event_info["reservations"]))

    @discord.ui.button(label="Clear Roles", style=discord.ButtonStyle.secondary, emoji="🧹", row=1)
    async def clear_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        role_id = get_server_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(role_id) if role_id else None
        if not role:
            await interaction.followup.send("❌ Event participant role has not been configured in /setup! Please contact a server administrator.", ephemeral=True)
            return
        
        count = 0
        for member in role.members:
            try: 
                await member.remove_roles(role)
                count += 1
            except Exception: pass
        await interaction.followup.send(f"🧹 Removed {role.mention} from **{count}** members!", ephemeral=True)

    @discord.ui.button(label="Conclude Event", style=discord.ButtonStyle.danger, emoji="🏁", row=1)
    async def conclude(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        event_info = db["events"].get(self.event_key)
        if event_info.get("concluded"):
            await interaction.followup.send("❌ This event has already been concluded!", ephemeral=True)
            return

        now = int(time.time())
        event_info["concluded"] = True
        event_info["concluded_at"] = now
        
        role_id = get_server_config(interaction.guild_id).get("event_role")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            for member in role.members:
                try: await member.remove_roles(role)
                except Exception: pass

        try:
            msg = await bot.get_channel(event_info["channel_id"]).fetch_message(event_info["message_ids"][0])
            await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild), view=None)
        except Exception: pass

        event_runtime_sec = max(0, now - event_info["end_time"])
        duration_hrs = round(event_runtime_sec / 3600, 1)
        player_count = len(event_info["reservations"])
        host_id = event_info["host_id"]
        
        hid = init_host(host_id)
        db["hosts"][hid]["total_events"] += 1
        db["hosts"][hid]["total_players"] += player_count
        save_data(db)

        stats_ch_id = get_server_config(interaction.guild_id).get("stats_channel")
        if stats_ch_id:
            stats_ch = interaction.guild.get_channel(stats_ch_id)
            if stats_ch:
                embed = discord.Embed(title="📊 EVENT RESULTS", color=0xF1C40F)
                embed.add_field(name="Event", value=f"**{event_info['display_name']}**", inline=False)
                embed.add_field(name="Host", value=f"<@{host_id}>", inline=True)
                embed.add_field(name="Match Duration", value=f"{duration_hrs} Hours", inline=True)
                embed.add_field(name="Players Joined", value=f"{player_count}", inline=True)
                embed.set_footer(text="⭐ Rating available for 1 hour after conclusion")

                class RateBtnView(discord.ui.View):
                    def __init__(self, h_id, e_key):
                        super().__init__(timeout=3600)
                        self.h_id = h_id
                        self.e_key = e_key

                    @discord.ui.button(label="Rate Host", style=discord.ButtonStyle.success, emoji="⭐")
                    async def rate(self, i: discord.Interaction, b: discord.ui.Button):
                        await i.response.send_modal(RateModal(self.h_id, self.e_key))

                await stats_ch.send(embed=embed, view=RateBtnView(host_id, self.event_key))

        await update_live_leaderboard(interaction.guild)
        await interaction.followup.send("🏁 Event successfully concluded, roles cleared, and stats posted!", ephemeral=True)

class EventButtonsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Spot", style=discord.ButtonStyle.success, emoji="🟢", custom_id="evt_btn_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        event_key, _ = get_event_by_message_id(interaction.message.id)
        if not event_key: return
        await interaction.response.send_modal(ClaimModal(event_key))

    @discord.ui.button(label="Swap Nation", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="evt_btn_swap")
    async def swap_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        event_key, _ = get_event_by_message_id(interaction.message.id)
        if not event_key: return
        last_time = swap_cooldowns.get(interaction.user.id, 0)
        remaining = 120 - (time.time() - last_time)
        if remaining > 0:
            await interaction.response.send_message(f"⏳ Swap cooldown active! Please wait **{int(remaining)}s** before sending another request.", ephemeral=True)
            return
        await interaction.response.send_modal(SwapModal(event_key))

    @discord.ui.button(label="Leave Event", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="evt_btn_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        event_key, event_info = get_event_by_message_id(interaction.message.id)
        if not event_key: return

        target = next((c for c, u in event_info["reservations"].items() if u["discord_id"] == interaction.user.id), None)
        if target:
            del event_info["reservations"][target]
            role_id = get_server_config(interaction.guild_id).get("event_role")
            if role_id:
                try: await interaction.user.remove_roles(interaction.guild.get_role(role_id))
                except Exception: pass

            target_channel = bot.get_channel(event_info["channel_id"])
            await process_waitlist_for_opened_spot(event_info, target, interaction.guild, target_channel)

            try:
                msg = await target_channel.fetch_message(event_info["message_ids"][0])
                await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
            except Exception: pass

            save_data(db)
            await send_audit_log(interaction.guild, "Reservation Cancelled", f"<@{interaction.user.id}> left **{event_info['display_name']}** (was **{target}**).")
            await interaction.followup.send("🗑️ Reservation removed.", ephemeral=True)
            return

        waitlist = event_info.get("waitlist", [])
        w_entry = next((w for w in waitlist if w["discord_id"] == interaction.user.id), None)
        if w_entry:
            waitlist.remove(w_entry)
            try:
                msg = await bot.get_channel(event_info["channel_id"]).fetch_message(event_info["message_ids"][0])
                await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, interaction.guild))
            except Exception: pass
            save_data(db)
            await interaction.followup.send("🗑️ Removed from the waitlist.", ephemeral=True)
            return

        await interaction.followup.send("❌ You do not have an active reservation or waitlist spot in this event! Contact a server host if you need help.", ephemeral=True)

    @discord.ui.button(label="Host Actions", style=discord.ButtonStyle.secondary, emoji="⚙️", custom_id="evt_btn_host_actions")
    async def host_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        event_key, _ = get_event_by_message_id(interaction.message.id)
        if not event_key: return
        if not is_host_or_admin(interaction):
            await interaction.response.send_message("❌ Host permissions required to manage this event! Please contact a server host if you need assistance.", ephemeral=True)
            return
        await interaction.response.send_message("⚙️ **Host Control Panel**", view=HostActionsView(event_key), ephemeral=True)

# ---------------------------------------------------------
# UNIFIED SETUP COMMAND 
# ---------------------------------------------------------

class HostRoleSelect(discord.ui.RoleSelect):
    def __init__(self): super().__init__(placeholder="Select Host Role")
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["host_role"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Host role set to {self.values[0].mention}!", ephemeral=True)

class EventRoleSelect(discord.ui.RoleSelect):
    def __init__(self): super().__init__(placeholder="Select Event Participant Role")
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["event_role"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Event participant role set to {self.values[0].mention}!", ephemeral=True)

class SuperpowerRoleSelect(discord.ui.RoleSelect):
    def __init__(self): super().__init__(placeholder="Select Superpower VIP Role")
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["superpower_role"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Superpower VIP role set to {self.values[0].mention}!", ephemeral=True)

class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self): super().__init__(placeholder="Select Audit Log Channel", channel_types=[discord.ChannelType.text])
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["log_channel"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Audit Log channel set to {self.values[0].mention}!", ephemeral=True)

class SwapChannelSelect(discord.ui.ChannelSelect):
    def __init__(self): super().__init__(placeholder="Select Swap Requests Channel", channel_types=[discord.ChannelType.text])
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["swap_channel"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Swap Requests channel set to {self.values[0].mention}!", ephemeral=True)

class StatsChannelSelect(discord.ui.ChannelSelect):
    def __init__(self): super().__init__(placeholder="Select Event Stats/Results Channel", channel_types=[discord.ChannelType.text])
    async def callback(self, interaction: discord.Interaction):
        config = get_server_config(interaction.guild_id)
        config["stats_channel"] = self.values[0].id
        save_data(db)
        await interaction.response.send_message(f"✅ Stats channel set to {self.values[0].mention}!", ephemeral=True)

class SetupRolePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(HostRoleSelect())
        self.add_item(EventRoleSelect())
        self.add_item(SuperpowerRoleSelect())

class SetupChannelPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(LogChannelSelect())
        self.add_item(SwapChannelSelect())
        self.add_item(StatsChannelSelect())

@bot.tree.command(name="setup", description="Master Admin Control Panel")
@app_commands.checks.has_permissions(administrator=True)
async def setup_command(interaction: discord.Interaction):
    config = get_server_config(interaction.guild_id)
    embed1 = discord.Embed(title="⚙️ Host Assistant Setup (1/2: Roles)", description=f"Host: <@&{config.get('host_role')}>\nEvent: <@&{config.get('event_role')}>\nVIP: <@&{config.get('superpower_role')}>", color=0x3498DB)
    await interaction.response.send_message(embed=embed1, view=SetupRolePanel(), ephemeral=True)
    
    embed2 = discord.Embed(title="⚙️ Host Assistant Setup (2/2: Channels)", description=f"Logs: <#{config.get('log_channel')}>\nSwaps: <#{config.get('swap_channel')}>\nStats: <#{config.get('stats_channel')}>", color=0x3498DB)
    await interaction.followup.send(embed=embed2, view=SetupChannelPanel(), ephemeral=True)

@setup_command.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permissions required to run /setup! Please contact a server administrator.", ephemeral=True)

# ---------------------------------------------------------
# BOT SLASH COMMANDS, TASKS & BOOTSTRAP
# ---------------------------------------------------------

@tasks.loop(seconds=30)
async def auto_refresh_banners():
    for event_key, event_info in list(db["events"].items()):
        if event_info.get("concluded"): continue
        channel_id = event_info.get("channel_id")
        msg_ids = event_info.get("message_ids", [])
        if not channel_id or not msg_ids: continue
        channel = bot.get_channel(channel_id)
        if not channel: continue
        try:
            guild = channel.guild
            msg = await channel.fetch_message(msg_ids[0])
            await msg.edit(embeds=build_event_embeds(event_info["display_name"], event_info, guild))
        except Exception: pass

async def setup_hook():
    bot.add_view(EventButtonsView())
    auto_refresh_banners.start()

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def prefix_sync(ctx):
    synced = await bot.tree.sync()
    await ctx.send(f"✅ Synced {len(synced)} slash command(s)!")

@prefix_sync.error
async def prefix_sync_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permissions to sync commands. Please contact a server administrator.")

@bot.tree.command(name="sync", description="Sync slash commands (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def sync_commands(interaction: discord.Interaction):
    synced = await bot.tree.sync()
    await interaction.response.send_message(f"✅ Synced {len(synced)} command(s)!", ephemeral=True)

@sync_commands.error
async def sync_commands_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permissions required to sync commands! Please contact a server administrator.", ephemeral=True)

async def active_event_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    for event_key, event_data in db["events"].items():
        if not event_data.get("concluded") and current.lower() in event_data["display_name"].lower():
            choices.append(app_commands.Choice(name=event_data["display_name"], value=event_data["display_name"]))
    return choices[:25]

@bot.tree.command(name="event", description="Create a new event reservation banner (Hosts/Admins only)")
@app_commands.describe(
    event_name="Event Name", 
    starts_in="Reservation delay before open, e.g. '0m', '15m'", 
    duration="Reservation open duration before lock, e.g. '30m', '1h'", 
    target_channel="Channel to post banner", 
    max_players="Optional max player limit (0 for unlimited)"
)
async def create_event(interaction: discord.Interaction, event_name: str, starts_in: str, duration: str, target_channel: discord.TextChannel, max_players: int = 0):
    await interaction.response.defer(ephemeral=True)

    if not is_host_or_admin(interaction):
        await interaction.followup.send("❌ Host permissions required to create events! Please contact a server host or administrator.", ephemeral=True)
        return

    start_offset = parse_duration(starts_in)
    duration_secs = parse_duration(duration)
    
    if duration_secs == 0 and start_offset == 0:
        duration_secs = 1800

    start_ts = int(time.time()) + start_offset
    end_ts = start_ts + duration_secs
    event_key = event_name.lower().strip()

    db["events"][event_key] = {
        "display_name": event_name, "start_time": start_ts, "end_time": end_ts,
        "host_id": interaction.user.id, "channel_id": target_channel.id,
        "message_ids": [], "superpowers": [], "reservations": {}, "waitlist": [],
        "voted_users": [], "max_players": max_players, "concluded": False
    }

    embeds = build_event_embeds(event_name, db["events"][event_key], interaction.guild)
    msg = await target_channel.send(embeds=embeds, view=EventButtonsView())
    db["events"][event_key]["message_ids"] = [msg.id]
    save_data(db)
    
    await send_audit_log(interaction.guild, "Event Created", f"Event **{event_name}** created by <@{interaction.user.id}>.")
    await interaction.followup.send("🎉 Event Created!", ephemeral=True)

@bot.tree.command(name="set_superpowers", description="Set superpower nations for an event")
@app_commands.autocomplete(event_name=active_event_autocomplete)
async def set_superpowers(interaction: discord.Interaction, event_name: str, nations: str):
    if not is_host_or_admin(interaction): 
        await interaction.response.send_message("❌ Host permissions required to set superpowers! Please contact a server host.", ephemeral=True)
        return
    event_key = event_name.lower().strip()
    if event_key not in db["events"]: 
        await interaction.response.send_message("❌ Could not find an active event with that name! Please contact a server host.", ephemeral=True)
        return
    sp_list = [n.strip().title() for n in nations.split(",") if n.strip()]
    db["events"][event_key]["superpowers"] = sp_list
    try:
        msg = await bot.get_channel(db["events"][event_key]["channel_id"]).fetch_message(db["events"][event_key]["message_ids"][0])
        await msg.edit(embeds=build_event_embeds(db["events"][event_key]["display_name"], db["events"][event_key], interaction.guild))
    except Exception: pass
    save_data(db)
    await interaction.response.send_message(f"⭐ Updated superpowers: {', '.join(sp_list)}", ephemeral=True)

@bot.tree.command(name="add_cohost", description="Assign a co-host to help manage an event")
@app_commands.autocomplete(event_name=active_event_autocomplete)
async def add_cohost(interaction: discord.Interaction, event_name: str, cohost: discord.Member):
    if not is_host_or_admin(interaction): 
        await interaction.response.send_message("❌ Host permissions required to add co-hosts! Please contact a server host.", ephemeral=True)
        return
    event_key = event_name.lower().strip()
    if event_key not in db["events"]: 
        await interaction.response.send_message("❌ Could not find an active event with that name! Please contact a server host.", ephemeral=True)
        return
    db["events"][event_key]["cohost_id"] = cohost.id
    try:
        msg = await bot.get_channel(db["events"][event_key]["channel_id"]).fetch_message(db["events"][event_key]["message_ids"][0])
        await msg.edit(embeds=build_event_embeds(db["events"][event_key]["display_name"], db["events"][event_key], interaction.guild))
    except Exception: pass
    save_data(db)
    await interaction.response.send_message(f"🤝 Assigned {cohost.mention} as co-host!", ephemeral=True)

@bot.tree.command(name="blacklist", description="Blacklist/Unblacklist a user from event reservations")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild_id)
    if guild_id not in db["blacklists"]: db["blacklists"][guild_id] = []
    if user.id in db["blacklists"][guild_id]:
        db["blacklists"][guild_id].remove(user.id)
        msg = f"🟢 Unblacklisted {user.mention}."
    else:
        db["blacklists"][guild_id].append(user.id)
        msg = f"🔴 Blacklisted {user.mention}."
    save_data(db)
    await interaction.response.send_message(msg, ephemeral=True)

@blacklist_command.error
async def blacklist_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permissions required to blacklist users! Please contact a server administrator.", ephemeral=True)

@bot.tree.command(name="spawn_leaderboard", description="Spawns the live-updating host leaderboard message")
@app_commands.checks.has_permissions(administrator=True)
async def spawn_leaderboard(interaction: discord.Interaction):
    embed = discord.Embed(title="🏆 Host Leaderboards", description="Updating...", color=0xF1C40F)
    msg = await interaction.channel.send(embed=embed)
    
    config = get_server_config(interaction.guild_id)
    config["leaderboard_msg_id"] = msg.id
    config["leaderboard_ch_id"] = interaction.channel_id
    save_data(db)
    
    await update_live_leaderboard(interaction.guild)
    await interaction.response.send_message("✅ Live leaderboard spawned!", ephemeral=True)

@spawn_leaderboard.error
async def spawn_leaderboard_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permissions required to spawn leaderboards! Please contact a server administrator.", ephemeral=True)

token = os.environ.get("BOT_TOKEN")
if token:
    keep_alive()
    bot.run(token)
else:
    print("❌ Error: BOT_TOKEN environment variable is not set!")
