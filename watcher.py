import os
import time
import asyncio
import json
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel
from telethon.utils import get_peer_id
import aiohttp

load_dotenv()

# --- Env ---
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_FILE = os.getenv("TELEGRAM_SESSION", "telegram_session")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip()

GROUP_INVITE = os.getenv("GROUP_INVITE", "").strip()
TARGET_USERNAME = os.getenv("TARGET_USERNAME", "").lstrip("@")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_CHAT_ID_ENV = os.getenv("BOT_CHAT_ID", "").strip()

NAG_INTERVAL_SECONDS = int(os.getenv("NAG_INTERVAL_SECONDS", "300"))
DEFAULT_NAG_INTERVAL_SECONDS = NAG_INTERVAL_SECONDS
NAG_MESSAGE_TEMPLATE = os.getenv(
    "NAG_MESSAGE_TEMPLATE",
    "🚨 ALERT: {who} just posted in {where}. Open Telegram now and register if needed. Reply /stop to this bot to stop alerts."
)
MAX_NAGS = int(os.getenv("MAX_NAGS", "0"))

KEYWORDS_RAW = os.getenv("REQUIRED_KEYWORDS", "").strip()
REQUIRED_KEYWORDS: List[str] = [k.strip().lower() for k in KEYWORDS_RAW.split(",") if k.strip()]

STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not API_ID or not API_HASH:
    raise SystemExit("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH.")
if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN from @BotFather.")

# --- Utils ---
def extract_invite_hash(link: str):
    if not link:
        return None
    s = link.strip()
    if "joinchat" in s:
        return s.rsplit("/", 1)[-1].split("?", 1)[0]
    if "/+" in s or s.startswith("https://t.me/+"):
        return s.split("+", 1)[-1].split("?", 1)[0]
    return None

async def resolve_group_entity(client: TelegramClient, value: str):
    if not value:
        raise ValueError("Empty group identifier.")
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return await client.get_entity(int(s))
    h = extract_invite_hash(s)
    if h:
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        from telethon.tl.types import ChatInviteAlready
        try:
            res = await client(CheckChatInviteRequest(h))
            if isinstance(res, ChatInviteAlready):
                return await client.get_entity(res.chat)
            upd = await client(ImportChatInviteRequest(h))
            if upd.chats:
                return upd.chats[0]
        except Exception:
            pass
    return await client.get_entity(s)

def ensure_group_entity(entity):
    return entity if isinstance(entity, (Chat, Channel)) else None

def build_message_link(peer_id: int, message_id: int) -> Optional[str]:
    if not peer_id or not message_id:
        return None
    s = str(peer_id)
    c = s[4:] if s.startswith("-100") else str(abs(int(s)))
    return f"https://t.me/c/{c}/{int(message_id)}"

def safe_slice(text: str, max_len: int = 3600) -> str:
    if not text:
        return ""
    return text if len(text) <= max_len else text[:max_len - 20] + "\n…(truncated)"

# --- Bot (polling) ---
class SimpleBot:
    def __init__(self, token: str, state_path: str, chat_id_env: str = ""):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.state_path = state_path
        self.session: Optional[aiohttp.ClientSession] = None
        self.update_offset = None
        self.chat_id: Optional[int] = None
        self.state: Dict[str, Any] = {}
        if chat_id_env:
            try: self.chat_id = int(chat_id_env)
            except: pass
        self._load_state()
    def _load_state(self):
        try:
            data = json.load(open(self.state_path, "r", encoding="utf-8"))
            if self.chat_id is None and data.get("bot_chat_id"): self.chat_id = int(data["bot_chat_id"])
            self.state.update({k:v for k,v in data.items() if k != "bot_chat_id"})
        except Exception:
            pass
    def _save_state(self):
        data = {"bot_chat_id": self.chat_id}; data.update(self.state)
        json.dump(data, open(self.state_path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    async def close(self):
        if self.session:
            await self.session.close(); self.session=None
    async def call(self, method: str, **params):
        url = f"{self.base}/{method}"
        async with self.session.post(url, data=params) as resp:
            text = await resp.text()
            if resp.status != 200: raise RuntimeError(f"Bot {method} HTTP {resp.status}: {text}")
            data = json.loads(text)
            if not data.get("ok"): raise RuntimeError(f"Bot {method} failed: {data}")
            return data["result"]
    async def send_message(self, text: str):
        if not self.chat_id:
            print("[BOT] No chat id yet. DM /start to register."); return
        try: return await self.call("sendMessage", chat_id=self.chat_id, text=text)
        except Exception as e: print(f"[BOT][ERROR] {e}")
    async def get_updates(self, timeout: int = 50):
        params = {"timeout": str(timeout)}
        if self.update_offset is not None: params["offset"] = str(self.update_offset)
        try: return await self.call("getUpdates", **params)
        except Exception as e: print(f"[BOT][WARN] getUpdates: {e}"); await asyncio.sleep(3); return []

# --- Alert state ---
class AlertState:
    def __init__(self):
        self.nag_active=False; self.nag_started_at=0.0; self.last_nag_sent_at=0.0; self.nag_count=0; self.reason=""
    def start(self, reason:str):
        self.nag_active=True; self.nag_started_at=time.time(); self.last_nag_sent_at=0.0; self.nag_count=0; self.reason=reason
    def stop(self): self.nag_active=False

# --- Main ---
async def main():
    global NAG_INTERVAL_SECONDS

    # client
    session = StringSession(STRING_SESSION) if STRING_SESSION else SESSION_FILE
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()

    # bot
    bot = SimpleBot(BOT_TOKEN, STATE_PATH, chat_id_env=BOT_CHAT_ID_ENV)
    await bot.start()

    # restore interval if saved
    si = bot.state.get("nag_interval")
    if isinstance(si, int) and si >= 30: NAG_INTERVAL_SECONDS = si

    # load saved group/user
    saved_group_link = bot.state.get("group_link")
    saved_group_peer_id = bot.state.get("group_peer_id")
    saved_target_id = bot.state.get("target_id")
    saved_target_username = bot.state.get("target_username")

    group = None; group_title = ""; group_peer_id = None

    # resolve group from state or env
    try_candidates = []
    if saved_group_link: try_candidates.append(("state_link", saved_group_link))
    if GROUP_INVITE: try_candidates.append(("env_link", GROUP_INVITE))
    for origin, val in try_candidates:
        try:
            ent = await resolve_group_entity(client, val)
            g = ensure_group_entity(ent)
            if g:
                group = g
                group_title = getattr(group, "title", None) or str(getattr(group, "id", "group"))
                group_peer_id = get_peer_id(group)
                print(f"[INFO] Monitoring group ({origin}): {group_title} (peer_id={group_peer_id})")
                bot.state["group_link"] = val
                bot.state["group_title"] = group_title
                bot.state["group_peer_id"] = group_peer_id
                bot._save_state()
                break
        except Exception as e:
            print(f"[WARN] Could not resolve {origin}: {e}")
    if group is None and saved_group_peer_id:
        # We still need the entity for title; try to resolve by dialogs
        async for d in client.iter_dialogs(limit=200):
            if get_peer_id(d.entity) == int(saved_group_peer_id):
                group = d.entity; group_title = d.name; group_peer_id = int(saved_group_peer_id)
                print(f"[INFO] Monitoring group (state peer): {group_title} (peer_id={group_peer_id})")
                break
    if group is None:
        print("[WARN] No group configured yet. Use /setgroup <invite|@public|id> in the bot chat.")

    # resolve target user
    target_id = None; target_username = None
    async def resolve_user(identifier: str):
        nonlocal target_id, target_username
        s = identifier.strip().lstrip("@")
        u = await client.get_entity(int(s)) if s.isdigit() else await client.get_entity(s)
        target_id = u.id; target_username = getattr(u, "username", None)

    if saved_target_id or saved_target_username:
        try: await resolve_user(str(saved_target_id or saved_target_username))
        except: pass
    if target_id is None and TARGET_USERNAME:
        try:
            await resolve_user(TARGET_USERNAME)
        except Exception as e:
            print(f"[WARN] TARGET_USERNAME couldn't be resolved: {e}. Use /setuser in the bot chat.")

    if target_id: print(f"[INFO] Watching for messages from: @{target_username or TARGET_USERNAME} (id={target_id})")

    alert = AlertState()

    # ---- bot loop ----
    async def bot_updates_loop():
        global NAG_INTERVAL_SECONDS
        nonlocal group, group_title, group_peer_id, target_id, target_username

        HELP = (
            "Commands:\n"
            "/start – register chat\n"
            "/stop – stop alerts\n"
            "/status – show status\n"
            "/interval <minutes>\n"
            "/setgroup <invite|@public|id>\n"
            "/listgroups\n"
            "/usegroup <peer_id>\n"
            "/setuser <@username|id>\n"
            "/reset\n"
            "/test\n"
        )
        while True:
            updates = await bot.get_updates(timeout=50)
            for upd in updates:
                bot.update_offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                chat = msg.get("chat", {}); text = (msg.get("text") or "").strip()
                args = text.split(); cmd = args[0].lower() if args else ""

                if cmd == "/start":
                    bot.chat_id = chat.get("id"); bot._save_state()
                    await bot.send_message("Registered. /help for commands.")
                    continue
                if cmd == "/help":
                    await bot.send_message(HELP); continue
                if cmd == "/stop":
                    alert.stop(); await bot.send_message("Alerts stopped."); continue
                if cmd == "/status":
                    status = "active" if alert.nag_active else "idle"
                    ginfo = f"{group_title} (peer_id={group_peer_id})" if group_peer_id else "unset"
                    uinfo = f"@{target_username or TARGET_USERNAME} (id={target_id})" if target_id else "unset"
                    await bot.send_message(
                        f"Status: {status}\n• Group: {ginfo}\n• User: {uinfo}\n• Interval: {NAG_INTERVAL_SECONDS}s\n• Cycle count: {alert.nag_count}"
                    )
                    continue
                if cmd == "/interval":
                    if len(args) >= 2:
                        try:
                            minutes = float(args[1]); NAG_INTERVAL_SECONDS = int(max(30, minutes*60))
                            bot.state["nag_interval"] = NAG_INTERVAL_SECONDS; bot._save_state()
                            await bot.send_message(f"Interval set to {minutes:g} min ({NAG_INTERVAL_SECONDS}s).")
                        except: await bot.send_message("Usage: /interval <minutes>")
                    else: await bot.send_message("Usage: /interval <minutes>")
                    continue
                if cmd == "/setgroup":
                    if len(args) >= 2:
                        try:
                            raw = " ".join(args[1:])
                            ent = await resolve_group_entity(client, raw)
                            g = ensure_group_entity(ent)
                            if not g:
                                await bot.send_message("That resolves to a USER, not a group. Use an invite link or /listgroups + /usegroup <peer_id>.")
                                continue
                            group = g; group_title = getattr(group,"title",None) or str(getattr(group,"id","group"))
                            group_peer_id = get_peer_id(group)
                            bot.state["group_link"] = raw; bot.state["group_title"] = group_title; bot.state["group_peer_id"] = group_peer_id; bot._save_state()
                            await bot.send_message(f"Group set: {group_title} (peer_id={group_peer_id})")
                        except Exception as e:
                            await bot.send_message(f"Could not set group: {e}")
                    else: await bot.send_message("Usage: /setgroup <invite|@public|id>")
                    continue
                if cmd == "/listgroups":
                    try:
                        lines = []
                        async for d in client.iter_dialogs(limit=100):
                            ent = d.entity
                            if isinstance(ent, (Chat, Channel)):
                                lines.append(f"{get_peer_id(ent)}\t{d.name}")
                        if not lines: await bot.send_message("No groups found.")
                        else:
                            buf=[]; 
                            for line in lines:
                                buf.append(line)
                                if len("\n".join(buf))>3500: await bot.send_message("Groups (peer_id\\ttitle):\n"+"\n".join(buf)); buf=[]
                            if buf: await bot.send_message("Groups (peer_id\\ttitle):\n"+"\n".join(buf))
                            await bot.send_message("Use /usegroup <peer_id> to switch.")
                    except Exception as e:
                        await bot.send_message(f"Could not list groups: {e}")
                    continue
                if cmd == "/usegroup":
                    if len(args) >= 2:
                        try:
                            ent = await resolve_group_entity(client, args[1])
                            g = ensure_group_entity(ent)
                            if not g:
                                await bot.send_message("That id resolves to a USER, not a group. Use a negative peer_id.")
                                continue
                            group = g; group_title = getattr(group,"title",None) or str(getattr(group,"id","group"))
                            group_peer_id = get_peer_id(group)
                            bot.state["group_link"] = ""  # set via id
                            bot.state["group_title"] = group_title
                            bot.state["group_peer_id"] = group_peer_id
                            bot._save_state()
                            await bot.send_message(f"Group set: {group_title} (peer_id={group_peer_id})")
                        except Exception as e:
                            await bot.send_message(f"Could not set group by id: {e}")
                    else: await bot.send_message("Usage: /usegroup <peer_id>")
                    continue
                if cmd == "/setuser":
                    if len(args) >= 2:
                        try:
                            who = args[1].lstrip("@")
                            u = await client.get_entity(int(who)) if who.isdigit() else await client.get_entity(who)
                            target_id = u.id; target_username = getattr(u,"username",None)
                            bot.state["target_id"] = target_id; bot.state["target_username"] = target_username or who; bot._save_state()
                            await bot.send_message(f"User set: @{target_username or who} (id={target_id})")
                        except Exception as e:
                            await bot.send_message(f"Could not set user: {e}")
                    else: await bot.send_message("Usage: /setuser <@username|id>")
                    continue
                if cmd == "/reset":
                    try:
                        alert.stop()
                        NAG_INTERVAL_SECONDS = DEFAULT_NAG_INTERVAL_SECONDS
                        bot.state["nag_interval"] = NAG_INTERVAL_SECONDS
                        if GROUP_INVITE:
                            ent = await resolve_group_entity(client, GROUP_INVITE)
                            g = ensure_group_entity(ent)
                            if not g: raise RuntimeError("GROUP_INVITE resolved to a USER.")
                            group = g; group_title = getattr(group,"title",None) or str(getattr(group,"id","group"))
                            group_peer_id = get_peer_id(group)
                            bot.state["group_link"]=GROUP_INVITE; bot.state["group_title"]=group_title; bot.state["group_peer_id"]=group_peer_id
                        if TARGET_USERNAME:
                            who = TARGET_USERNAME.lstrip("@")
                            u = await client.get_entity(int(who)) if who.isdigit() else await client.get_entity(who)
                            target_id = u.id; target_username = getattr(u,"username",None)
                            bot.state["target_id"]=target_id; bot.state["target_username"]=target_username or who
                        bot._save_state()
                        await bot.send_message("Reset done.")
                    except Exception as e:
                        await bot.send_message(f"Reset failed: {e}")
                    continue
                if cmd == "/test":
                    alert.start(f"Manual test at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    await bot.send_message("Test alerts started. Send /stop to stop.")
                    continue
            await asyncio.sleep(0.5)

    # ---- nag loop ----
    async def nag_loop():
        while True:
            if alert.nag_active and bot.chat_id:
                now = time.time()
                if alert.last_nag_sent_at == 0.0 or (now - alert.last_nag_sent_at) >= NAG_INTERVAL_SECONDS:
                    who = f"@{target_username or TARGET_USERNAME}" if target_id else "(unset user)"
                    where = group_title or "(unset group)"
                    await bot.send_message(NAG_MESSAGE_TEMPLATE.format(who=who, where=where))
                    alert.last_nag_sent_at = now; alert.nag_count += 1
                    if MAX_NAGS and alert.nag_count >= MAX_NAGS:
                        alert.stop(); await bot.send_message("⛔ Max nags reached.")
            await asyncio.sleep(1.0)

    # ---- triggers ----
    @client.on(events.NewMessage)
    async def on_new_message(event):
        if not group_peer_id or not target_id: return
        if event.chat_id != group_peer_id: return  # *** FIX: compare peer ids ***

        try: sender = await event.get_sender()
        except Exception: sender = None

        is_from_target = False
        if sender:
            if sender.id == target_id: is_from_target = True
            elif target_username and getattr(sender,"username",None):
                is_from_target = sender.username.lower() == target_username.lower()

        if not is_from_target: return

        # optional keyword filter
        if REQUIRED_KEYWORDS:
            body = (event.raw_text or "").lower()
            if not any(k in body for k in REQUIRED_KEYWORDS): return

        when = event.date.strftime("%Y-%m-%d %H:%M:%S") if event.date else "now"
        alert.start(f"Message from @{target_username or TARGET_USERNAME} in {group_title} at {when}")

        if bot.chat_id:
            text = event.raw_text or "(no text)"
            link = build_message_link(group_peer_id, getattr(event, 'id', None))
            header = f"📨 Forwarded message\nFrom @{target_username or TARGET_USERNAME} in {group_title}\n🕒 {when}"
            body = safe_slice(text, 3600)
            tail = f"\n🔗 Open: {link}" if link else ""
            media_note = "\n📎 (media present but not forwarded)" if event.message and event.message.media else ""
            await bot.send_message(f"{header}\n\n{body}{media_note}{tail}")

        who = f"@{target_username or TARGET_USERNAME}"
        await bot.send_message(f"🚨 Trigger: {who} posted in {group_title} at {when}. I'll keep pinging you until you /stop.")

    print("[READY] Listener is live. DM /start to your bot, then /setgroup and /setuser.")
    await asyncio.gather(client.run_until_disconnected(), bot_updates_loop(), nag_loop())

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n[EXIT] Stopped by user.")
