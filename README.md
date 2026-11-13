# Host Assistant bot

Start command is unchanged: `python main.py`

```
main.py                 starts the bot (imports everything below, then runs)
hostbot/
  core.py               bot object, settings (keys, owner IDs, AI models, personas),
                        database, server whitelist, trust checks
  shared.py             helpers used everywhere: permissions, notices, logging,
                        event banner embed, per-event log + waitlist
  events.py             event buttons, claim/swap/rate, host panel (conclude/cancel),
                        /event /set_superpowers /cohost /edit_event
                        /blacklist add|remove|list /warn /warnings /unwarn
                        /trust /trusted_list
  hosts.py              achievements, /host /me /achievements /analytics,
                        server enable/disable, trial-host voting
  ai.py                 chatbot, /clearchat, Groq/OpenRouter, AI admin engine
  server_tools.py       /setup wizard and /talk
  panels.py             /post_panel (builds the styled info messages)
  panel_data.py         <- your info panel text lives here
  players.py            /player profiles
  routines.py           event reminders, weekly summary, 90-day cleanup
  lifecycle.py          timers, on_ready, !sync
```

Biggest file is ai.py (~1,400 lines). To change one thing, share only that file.
If you add a new file with commands, add its name to the import line in main.py.
Values that change while the bot runs (bot_muted, RATE_LIMIT_TRIGGERED, ai_worker_task)
live in core.py and are used as core.X from other files.
