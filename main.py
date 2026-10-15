import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
import time
from flask import Flask
from threading import Thread

# Flask Web Server to keep Render active
app = Flask('')

@app.route('/')
def home():
    return "Bot is live!"

def run():
    # Dynamic port for Render (defaults to 10000)
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

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"server_config": {}, "events": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_data()
if "server_config" not in db:
    db["server_config"] = {}
if "events" not in db:
    db["events"] = {}

COLOR_PALETTE = [0x9B59B6, 0xF1C40F, 0x1ABC9C, 0xE91E63, 0x2ECC71, 0x3498DB]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error syncing commands: {e}")

# Helper check for host/admin roles
def is_host_or_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    
    guild_id = str(interaction.guild_id)
    config = db["server_config"].get(guild_id, {})
    host_role_id = config.get("host_role")
    
    if host_role_id:
        user_role_ids = [role.id for role in interaction.user.roles]
        if host_role_id in user_role_ids:
            return True
            
    return False

def build_event_embeds(event_name, event_info):
    start_ts = event_info["start_time"]
    end_ts = event_info["end_time"]
    host_id = event_info["host_id"]
    reservations = event_info["reservations"]
    color = event_info.get("color", 0x9B59B6)

    lines = []
    for idx, (country, user_data) in enumerate(reservations.items(), 1):
        lines.append(f"┆ `{idx:02d}` 🚩 **{country}** ➔ `{user_data['roblox']}` (<@{user_data['discord_id']}>)")

    if not lines:
        lines = ["*✨ No reservations claimed yet! Be the first using `/reserve`!*"]

    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        if current_length + len(line) > 800:
            chunks.append(current_chunk)
            current_chunk = [line]
            current_length = len(line)
        else:
            current_chunk.append(line)
            current_length += len(line) + 1
    if current_chunk:
        chunks.append(current_chunk)

    total_parts = len(chunks)
    embeds = []

    for idx, chunk in enumerate(chunks, 1):
        embed = discord.Embed(color=color)
        if idx == 1:
            embed.title = f"⚔️  ───  〔 {event_name.upper()} 〕  ───  ⚔️"
            embed.description = (
                f"```fix\n"
                f"🎮 EVENT RESERVATIONS & SCHEDULE\n"
                f"```\n"
                f"⏰ **Starts:** <t:{start_ts}:F> (<t:{start_ts}:R>)\n"
                f"🏁 **Ends:** <t:{end_ts}:F> (<t:{end_ts}:R>)\n"
                f"👑 **Host:** <@{host_id}>\n"
                f"─────────────\n\n"
                f"### 🎯 CLAIMED NATIONS\n" + "\n".join(chunk)
            )
        else:
            embed.title = f"⚔️  ───  〔 {event_name.upper()} | PART {idx}/{total_parts} 〕  ───  ⚔️"
            embed.description = f"### 🎯 CLAIMED NATIONS (Cont.)\n" + "\n".join(chunk)

        total_count = len(reservations)
        embed.set_footer(text=f"🔥 Total Joined: {total_count} Players")
        embeds.append(embed)

    return embeds

async def active_event_autocomplete(
    interaction: discord.Interaction, 
    current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    current_time = int(time.time())
    
    for event_key, event_data in db["events"].items():
        display_name = event_data["display_name"]
        end_time = event_data.get("end_time", 0)
        
        if end_time > current_time and current.lower() in display_name.lower():
            choices.append(app_commands.Choice(name=display_name, value=display_name))
            
    return choices[:25]

@bot.tree.command(name="set_event_role", description="Set the role given to event participants (Admins only)")
@app_commands.describe(role="The role given when someone reserves")
@app_commands.checks.has_permissions(administrator=True)
async def set_event_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)
    if guild_id not in db["server_config"]:
        db["server_config"][guild_id] = {}
    db["server_config"][guild_id]["event_role"] = role.id
    save_data(db)
    await interaction.response.send_message(f"✅ Official event participant role set to {role.mention}!", ephemeral=True)

@bot.tree.command(name="set_host_role", description="Set the role allowed to host/manage events (Admins only)")
@app_commands.describe(role="The role allowed to create and manage events")
@app_commands.checks.has_permissions(administrator=True)
async def set_host_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)
    if guild_id not in db["server_config"]:
        db["server_config"][guild_id] = {}
    db["server_config"][guild_id]["host_role"] = role.id
    save_data(db)
    await interaction.response.send_message(f"✅ Official host role set to {role.mention}!", ephemeral=True)

@bot.tree.command(name="event", description="Create a new event reservation banner (Hosts/Admins only)")
@app_commands.describe(
    event_name="Event Name (e.g. Modern Eurasia)",
    start_time="Start Unix timestamp",
    end_time="End Unix timestamp",
    target_channel="Channel to post reservations embed"
)
async def create_event(
    interaction: discord.Interaction, 
    event_name: str, 
    start_time: str, 
    end_time: str, 
    target_channel: discord.TextChannel
):
    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have the required Host role to create events!", ephemeral=True)
        return

    try:
        start_ts = int(start_time)
        end_ts = int(end_time)
    except ValueError:
        await interaction.response.send_message("❌ Timestamps must be numbers!", ephemeral=True)
        return

    event_key = event_name.lower().strip()
    selected_color = random.choice(COLOR_PALETTE)
    
    db["events"][event_key] = {
        "display_name": event_name,
        "start_time": start_ts,
        "end_time": end_ts,
        "host_id": interaction.user.id,
        "channel_id": target_channel.id,
        "color": selected_color,
        "message_ids": [],
        "reservations": {}
    }

    embeds = build_event_embeds(event_name, db["events"][event_key])
    sent_msg = await target_channel.send(embeds=embeds)
    db["events"][event_key]["message_ids"] = [sent_msg.id]
    
    save_data(db)
    await interaction.response.send_message(f"🎉 Created event **{event_name}** in {target_channel.mention}!", ephemeral=True)

@bot.tree.command(name="reserve", description="Claim your spot for an event!")
@app_commands.describe(
    event_name="Event Name",
    country="Country/Nation to claim",
    robloxuser="Your Roblox Username"
)
@app_commands.autocomplete(event_name=active_event_autocomplete)
async def reserve(
    interaction: discord.Interaction, 
    event_name: str, 
    country: str, 
    robloxuser: str
):
    event_key = event_name.lower().strip()
    
    if event_key not in db["events"]:
        await interaction.response.send_message("❌ Active event not found!", ephemeral=True)
        return

    event_info = db["events"][event_key]
    
    if int(time.time()) >= event_info["end_time"]:
        await interaction.response.send_message("❌ This event has already ended!", ephemeral=True)
        return

    if country.title() in event_info["reservations"]:
        await interaction.response.send_message(f"🚫 **{country.title()}** has already been claimed!", ephemeral=True)
        return

    for c_name, u_data in event_info["reservations"].items():
        if u_data["discord_id"] == interaction.user.id:
            await interaction.response.send_message(f"🚫 You have already reserved **{c_name}**!", ephemeral=True)
            return

    event_info["reservations"][country.title()] = {
        "roblox": robloxuser,
        "discord_id": interaction.user.id
    }

    guild_id = str(interaction.guild_id)
    config = db["server_config"].get(guild_id, {})
    role_id = config.get("event_role") if isinstance(config, dict) else config
    
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await interaction.user.add_roles(role)
            except Exception as e:
                print(f"Failed to assign role: {e}")

    target_channel = bot.get_channel(event_info["channel_id"])
    embeds = build_event_embeds(event_info["display_name"], event_info)

    try:
        first_msg = await target_channel.fetch_message(event_info["message_ids"][0])
        await first_msg.edit(embeds=embeds)
    except Exception as e:
        print(f"Failed to update embed: {e}")

    save_data(db)
    await interaction.response.send_message(f"🚀 Reserved **{country.title()}** for **{robloxuser}**!", ephemeral=True)

@bot.tree.command(name="unreserve", description="Cancel your reservation for an event")
@app_commands.describe(event_name="Event Name")
@app_commands.autocomplete(event_name=active_event_autocomplete)
async def unreserve(interaction: discord.Interaction, event_name: str):
    event_key = event_name.lower().strip()
    
    if event_key not in db["events"]:
        await interaction.response.send_message("❌ Event not found!", ephemeral=True)
        return

    event_info = db["events"][event_key]
    user_id = interaction.user.id
    target_country = None

    for country, u_data in event_info["reservations"].items():
        if u_data["discord_id"] == user_id:
            target_country = country
            break

    if not target_country:
        await interaction.response.send_message("❌ You do not have a reservation in this event!", ephemeral=True)
        return

    del event_info["reservations"][target_country]

    guild_id = str(interaction.guild_id)
    config = db["server_config"].get(guild_id, {})
    role_id = config.get("event_role") if isinstance(config, dict) else config
    
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await interaction.user.remove_roles(role)
            except Exception as e:
                print(f"Failed to remove role: {e}")

    target_channel = bot.get_channel(event_info["channel_id"])
    embeds = build_event_embeds(event_info["display_name"], event_info)
    try:
        first_msg = await target_channel.fetch_message(event_info["message_ids"][0])
        await first_msg.edit(embeds=embeds)
    except Exception as e:
        print(f"Failed to update embed: {e}")

    save_data(db)
    await interaction.response.send_message(f"🗑️ Removed your reservation for **{target_country}**.", ephemeral=True)

@bot.tree.command(name="kick_reservation", description="Force-remove a reservation (Hosts/Admins only)")
@app_commands.describe(event_name="Event Name", country="Country/Nation to clear")
@app_commands.autocomplete(event_name=active_event_autocomplete)
async def kick_reservation(interaction: discord.Interaction, event_name: str, country: str):
    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have the required Host role to remove reservations!", ephemeral=True)
        return

    event_key = event_name.lower().strip()
    
    if event_key not in db["events"]:
        await interaction.response.send_message("❌ Event not found!", ephemeral=True)
        return

    event_info = db["events"][event_key]
    c_title = country.title()

    if c_title not in event_info["reservations"]:
        await interaction.response.send_message(f"❌ **{c_title}** is not currently reserved!", ephemeral=True)
        return

    removed_user_id = event_info["reservations"][c_title]["discord_id"]
    del event_info["reservations"][c_title]

    guild_id = str(interaction.guild_id)
    config = db["server_config"].get(guild_id, {})
    role_id = config.get("event_role") if isinstance(config, dict) else config
    
    if role_id:
        role = interaction.guild.get_role(role_id)
        member = interaction.guild.get_member(removed_user_id)
        if role and member:
            try:
                await member.remove_roles(role)
            except Exception as e:
                print(f"Failed to remove role: {e}")

    target_channel = bot.get_channel(event_info["channel_id"])
    embeds = build_event_embeds(event_info["display_name"], event_info)
    try:
        first_msg = await target_channel.fetch_message(event_info["message_ids"][0])
        await first_msg.edit(embeds=embeds)
    except Exception as e:
        print(f"Failed to update embed: {e}")

    save_data(db)
    await interaction.response.send_message(f"⚡ Cleared reservation for **{c_title}**!", ephemeral=True)

@bot.tree.command(name="clearroles", description="Safely strip the configured event role from everyone (Hosts/Admins only)")
async def clear_roles(interaction: discord.Interaction):
    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission to clear roles!", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    config = db["server_config"].get(guild_id, {})
    role_id = config.get("event_role") if isinstance(config, dict) else config

    if not role_id:
        await interaction.response.send_message("❌ No event role has been configured yet! Run `/set_event_role` first.", ephemeral=True)
        return

    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Configured event role was deleted from the server!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    count = 0

    for member in role.members:
        try:
            await member.remove_roles(role)
            count += 1
        except Exception as e:
            print(f"Error stripping role: {e}")

    await interaction.followup.send(f"🧹 Successfully removed {role.mention} from **{count}** members!", ephemeral=True)

# Start web server then launch bot
keep_alive()
bot.run("MTU2MDE5MDY3MTU2MDc3MzYzMg.GtuBUN.aZFq5hFAMdIGw3_NaJ2eQQCBkyEE0qGk1xtEDs")
