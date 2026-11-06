# Connected Discord-GitHub | Discord: AbyssalNoob1 | Roblox: AbyssalNoob1


import discord
import os
import time

from hostbot.core import bot, keep_alive
from hostbot import core

# Importing these registers every command, event and view with the bot.
from hostbot import ai, events, hosts, server_tools, lifecycle, panels  # noqa: F401

tk = os.environ.get("BOT_TOKEN")
if tk:
    keep_alive()
    try:
        bot.run(tk, reconnect=False)
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            core.RATE_LIMIT_TRIGGERED = True
            print("🚨 Initial Discord login was rate-limited (429). Bot will remain offline instead of restart-looping.")
        else:
            raise
    if core.RATE_LIMIT_TRIGGERED:
        print("🛑 Host Assistant is in safe hold mode. No further Discord requests will be made.")
        while True:
            time.sleep(3600)
else:
    print("No token set!")
