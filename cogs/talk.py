# cogs/talk.py
import discord
from discord import app_commands
from discord.ext import commands
import time
import datetime
from database import db, get_config

BOT_OWNER_IDS = [1359263076838539296, 1298335881647620108]
ALLOWED_USER_IDS = [1359263076838539296]
TALK_COOLDOWN = 4
_talk_cooldowns = {}
DIV = "~~" + "\u2800" * 25 + "~~"

def is_trusted(member):
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return member.id in db.get("trusted_users", {}).get(str(guild.id), [])

def has_host_role(member, guild_id):
    host_role_id = get_config(guild_id).get("host_role")
    return bool(host_role_id) and any(r.id == host_role_id for r in getattr(member, "roles", []))

def can_use_talk(member, guild_id):
    if member is None:
        return False
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    if is_trusted(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    if has_host_role(member, guild_id):
        return True
    vip_role = get_config(guild_id).get("superpower_role")
    return bool(vip_role) and any(r.id == vip_role for r in getattr(member, "roles", []))

async def log_talk_usage(guild, author, channel, message):
    log_id = get_config(guild.id).get("talk_log_channel")
    if not log_id:
        return
    log_channel = guild.get_channel(log_id)
    if not log_channel:
        return
    safe_message = discord.utils.escape_mentions(str(message))
    embed = discord.Embed(
        title="💬 /talk · Message Sent",
        description=f"{DIV}\n### 📝 Message\n> {safe_message.replace(chr(10), chr(10) + '> ')[:1800]}",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    if author.display_avatar:
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    else:
        embed.set_author(name=str(author))
    embed.add_field(name="👤 Author", value=f"{author.mention}\n`{author.id}`", inline=True)
    embed.add_field(name="📍 Channel", value=f"{channel.mention}\n`{channel.id}`", inline=True)
    embed.set_footer(text=f"Host Assistant · {guild.name}")
    try:
        await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        print("/talk log failed:", e)

class TalkCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="talk", description="Make Host Assistant send a message as the bot")
    @app_commands.guild_only()
    @app_commands.describe(message="Exactly what Host Assistant should say")
    async def talk_cmd(self, interaction: discord.Interaction, message: str):
        if not can_use_talk(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "🔒 You need the Host role, VIP role, Administrator permission, or trusted access to use /talk.",
                ephemeral=True
            )
            return

        message = message.strip()
        if not message:
            await interaction.response.send_message("❌ Give me something to say.", ephemeral=True)
            return
        if len(message) > 2000:
            await interaction.response.send_message("❌ That message is too long. Discord messages are limited to 2,000 characters.", ephemeral=True)
            return

        now = time.time()
        last = _talk_cooldowns.get(interaction.user.id, 0)
        if now - last < TALK_COOLDOWN:
            remaining = max(1, int(TALK_COOLDOWN - (now - last)))
            await interaction.response.send_message(f"⏳ Slow down a little. Try again in {remaining}s.", ephemeral=True)
            return
        _talk_cooldowns[interaction.user.id] = now

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none()
            )
            await log_talk_usage(interaction.guild, interaction.user, interaction.channel, message)
            await interaction.edit_original_response(content="✨ Sent successfully.")
        except Exception as e:
            print("/talk error:", e)
            try:
                await interaction.edit_original_response(content="❌ I couldn't send that message.")
            except Exception:
                pass

async def setup(bot):
    await bot.add_cog(TalkCog(bot))
