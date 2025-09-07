# Local tester

This tester checks your environment, your bot token, Telethon login, resolves the group, and can drive an **end‑to‑end** trigger by:
1) Telling the watcher to monitor the chosen group and **you** as the user
2) Sending a test message into the group from your account

> Run the tester **while `watcher.py` is running** in another terminal.

## Steps

```bash
# 1) Install deps (once)
pip install -r requirements.txt

# 2) Start the watcher in Terminal 1
python watcher.py

# 3) In Telegram, DM /start to your bot (once)

# 4) In Terminal 2, run the tester
python tester.py
```

What you should see:
- Bot token OK
- Telethon login OK (shows your username)
- Group resolved (title + a **negative** id like `-100...`)
- Tester sends `/usegroup <id>` and `/setuser @<you>` to the bot chat (so the watcher starts watching **you**)
- Tester posts a message in the group
- **Result:** You receive a DM from the watcher (forwarded text + then nags every N minutes) until you `/stop`

> If the tester says there's no `bot_chat_id`, first open the bot and press **Start**, or set `BOT_CHAT_ID` in `.env`.

### Troubleshooting

- If the bot never replies to commands:
  - Check the watcher console for `[BOT][WARN] getUpdates failed: ...`. If you previously used **webhooks** with this bot, run:
    ```
    curl -s -X POST https://api.telegram.org/bot<YOUR_TOKEN>/deleteWebhook
    ```
  - Make sure you pressed **Start** in the bot chat and haven’t muted the bot.

- If group resolution fails:
  - Ensure you are **a member** of the target group.
  - Use `/listgroups` in the bot chat to find the correct **negative** group id, then `/usegroup <that_id>`.

- If the trigger doesn’t happen:
  - Make sure the watcher is running **and** the target user is set to **you** during the test (the tester sends `/setuser` for you).
  - If you configured `REQUIRED_KEYWORDS` in `.env`, include one of those words in your test message or temporarily clear that setting.
