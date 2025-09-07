import os, json, time, asyncio
from dotenv import load_dotenv
import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Chat, Channel
from telethon.utils import get_peer_id

load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_FILE = os.getenv("TELEGRAM_SESSION", "telegram_session")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_INVITE = os.getenv("GROUP_INVITE", "").strip()
STATE_PATH = os.getenv("STATE_PATH", "state.json")
BOT_CHAT_ID_ENV = os.getenv("BOT_CHAT_ID", "").strip()

def fail(msg): print("❌", msg); raise SystemExit(1)

async def bot_call(session, method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with session.post(url, data=params) as resp:
        text = await resp.text()
        if resp.status != 200: fail(f"Bot {method} HTTP {resp.status}: {text}")
        data = json.loads(text)
        if not data.get("ok"): fail(f"Bot {method} failed: {data}")
        return data["result"]

async def main():
    print("== Tester starting ==")
    if not API_ID or not API_HASH: fail("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH")
    if not BOT_TOKEN: fail("Missing BOT_TOKEN")
    async with aiohttp.ClientSession() as http:
        me = await bot_call(http, "getMe")
        print(f"✓ Bot token OK: @{me['username']} (id={me['id']})")

    bot_chat_id = None
    if os.path.exists(STATE_PATH):
        try:
            st = json.load(open(STATE_PATH, "r", encoding="utf-8"))
            if "bot_chat_id" in st: bot_chat_id = int(st["bot_chat_id"])
        except: pass
    if not bot_chat_id and BOT_CHAT_ID_ENV:
        try: bot_chat_id = int(BOT_CHAT_ID_ENV)
        except: pass
    if not bot_chat_id: print("⚠️  No bot_chat_id. DM /start to the bot once.")

    session = StringSession(STRING_SESSION) if STRING_SESSION else SESSION_FILE
    client = TelegramClient(session, API_ID, API_HASH); await client.start()
    me_user = await client.get_me()
    my_username = getattr(me_user, "username", None)
    print(f"✓ Telethon login OK as @{my_username or me_user.id}")

    # resolve group (prefer env)
    ident = GROUP_INVITE
    if not ident:
        if os.path.exists(STATE_PATH):
            try:
                s = json.load(open(STATE_PATH,"r",encoding="utf-8"))
                ident = s.get("group_link","")
            except: pass
    if not ident: fail("No GROUP_INVITE or saved group_link. Set one, or /setgroup in bot chat.")

    ent = await client.get_entity(ident)
    if not isinstance(ent, (Chat, Channel)): fail("Resolved entity is not a group/supergroup.")
    title = getattr(ent, "title", None) or str(getattr(ent, "id", ""))
    peer_id = get_peer_id(ent)
    print(f"✓ Group resolved: {title} (peer_id={peer_id})")

    if bot_chat_id:
        async with aiohttp.ClientSession() as http:
            await bot_call(http, "sendMessage", chat_id=bot_chat_id, text=f"/usegroup {peer_id}")
            if my_username:
                await bot_call(http, "sendMessage", chat_id=bot_chat_id, text=f"/setuser @{my_username}")
            else:
                await bot_call(http, "sendMessage", chat_id=bot_chat_id, text=f"/setuser {me_user.id}")
            await bot_call(http, "sendMessage", chat_id=bot_chat_id, text="/status")
            print("✓ Sent /usegroup (peer_id) and /setuser to watcher.")

    msg = f"[SELFTEST] If watcher is running, it should DM and nag. ts={int(time.time())}"
    await client.send_message(entity=ent, message=msg)
    print("✓ Sent a test message to the group.")
    print("👉 Watcher should DM you (forwarded message) and start nagging.")
    print("== Tester finished ==")

if __name__ == "__main__":
    asyncio.run(main())
