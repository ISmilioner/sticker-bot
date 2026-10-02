"""Sticker pack bot for PaxtaGram.
Run:  pip install httpx
      BOT_TOKEN=123:abc python bot.py
Optional env: PACK_LINK_TPL  (default https://paxta.online/{username}?start={code})
"""
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://paxta.online/bot{TOKEN}/"
LINK_TPL = os.getenv("PACK_LINK_TPL", "https://paxta.online/{username}?start={code}")
http = httpx.Client(timeout=40)

DATABASE_URL = os.getenv("DATABASE_URL")  # bo'lsa Postgres (Neon), bo'lmasa SQLite
PG = bool(DATABASE_URL)
PK = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY"


class DB:
    def __init__(self):
        self.conn = None
        self.connect()

    def connect(self):
        if PG:
            import psycopg
            self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
        else:
            self.conn = sqlite3.connect("stickers.db", check_same_thread=False,
                                        isolation_level=None)

    def execute(self, sql, args=()):
        if PG:
            sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                return self.conn.execute(sql, args)
            except Exception as e:
                if attempt == 2 or not PG:
                    raise
                print("db reconnect:", e)
                self.connect()

    def commit(self):
        pass  # autocommit yoqilgan


db = DB()
db.execute(f"CREATE TABLE IF NOT EXISTS packs(id {PK}, owner BIGINT, title TEXT, code TEXT UNIQUE)")
db.execute(f"CREATE TABLE IF NOT EXISTS items(id {PK}, pack_id INTEGER, file_id TEXT, kind TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS state(user_id BIGINT PRIMARY KEY, mode TEXT, pack_id INTEGER)")
BOT_USER = ""


def api(method, **params):
    try:
        r = http.post(API + method, json=params).json()
    except Exception as e:
        print("net error", method, e)
        return None
    if not r.get("ok"):
        print("api error", method, r)
        return None
    return r["result"]


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def btn(text, data):
    return {"text": text, "callback_data": data}


def send(chat, text, markup=None):
    return api("sendMessage", chat_id=chat, text=text, reply_markup=markup)


def set_state(uid, mode=None, pack_id=None):
    db.execute("INSERT INTO state VALUES(?,?,?) ON CONFLICT (user_id) DO UPDATE "
               "SET mode=excluded.mode, pack_id=excluded.pack_id", (uid, mode, pack_id))
    db.commit()


def get_state(uid):
    row = db.execute("SELECT mode, pack_id FROM state WHERE user_id=?", (uid,)).fetchone()
    return row or (None, None)


def get_pack(pid):
    return db.execute("SELECT id, owner, title, code FROM packs WHERE id=?", (pid,)).fetchone()


def count_items(pid):
    return db.execute("SELECT COUNT(*) FROM items WHERE pack_id=?", (pid,)).fetchone()[0]


def link(code):
    return LINK_TPL.format(username=BOT_USER, code=code)


def menu(chat):
    send(chat, "Sticker pack bot. Tanlang:",
         kb([btn("➕ Yangi pack", "new")], [btn("📦 Packlarim", "list")]))


def pack_card(chat, pid, viewer):
    p = get_pack(pid)
    if not p:
        return send(chat, "Pack topilmadi.")
    text = f"📦 {p[2]}\nRasmlar: {count_items(pid)}\n🔗 {link(p[3])}\nKod: /start p_{p[3]}"
    rows = [[btn("👁 Ko'rish", f"view:{pid}")]]
    if viewer == p[1]:
        rows = [
            [btn("➕ Qo'shish", f"add:{pid}"), btn("👁 Ko'rish", f"view:{pid}")],
            [btn("✏️ Nomi", f"ren:{pid}"), btn("🗑 O'chirish", f"delp:{pid}")],
        ]
    send(chat, text, {"inline_keyboard": rows})


def show_items(chat, pid, viewer):
    p = get_pack(pid)
    items = db.execute("SELECT id, file_id, kind FROM items WHERE pack_id=?", (pid,)).fetchall()
    if not items:
        return send(chat, "Pack bo'sh.")
    for iid, fid, kind in items:
        markup = kb([btn("🗑", f"deli:{iid}")]) if p and viewer == p[1] else None
        if kind == "photo":
            api("sendPhoto", chat_id=chat, photo=fid, reply_markup=markup)
        else:
            api("sendDocument", chat_id=chat, document=fid, reply_markup=markup)
        time.sleep(0.05)  # limit: 30 req/s


def on_message(m):
    uid, chat = m["from"]["id"], m["chat"]["id"]
    text = m.get("text") or ""
    mode, pid = get_state(uid)

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("p_"):
            row = db.execute("SELECT id FROM packs WHERE code=?", (parts[1][2:],)).fetchone()
            return pack_card(chat, row[0], uid) if row else send(chat, "Pack topilmadi.")
        set_state(uid)
        return menu(chat)
    if text.startswith("/newpack"):
        set_state(uid, "title")
        return send(chat, "Pack nomini yozing:")
    if text.startswith("/mypacks"):
        return list_packs(chat, uid)
    if text.startswith("/done"):
        set_state(uid)
        return pack_card(chat, pid, uid) if pid else menu(chat)

    kind = fid = None
    if m.get("photo"):
        best = max(m["photo"], key=lambda x: x.get("file_size") or x.get("width", 0))
        kind, fid = "photo", best["file_id"]
    elif m.get("document") and (m["document"].get("mime_type") or "").startswith("image/"):
        kind, fid = "document", m["document"]["file_id"]

    if fid:
        if mode != "adding" or not pid:
            return send(chat, "Avval packni tanlang: /mypacks yoki /newpack")
        db.execute("INSERT INTO items(pack_id, file_id, kind) VALUES(?,?,?)", (pid, fid, kind))
        db.commit()
        return send(chat, f"✅ Qo'shildi ({count_items(pid)}). Yana yuboring yoki tugating.",
                    kb([btn("✅ Tayyor", f"open:{pid}")]))

    if text and mode == "title":
        code = secrets.token_hex(4)
        cur = db.execute("INSERT INTO packs(owner, title, code) VALUES(?,?,?) RETURNING id",
                         (uid, text[:64], code))
        set_state(uid, "adding", cur.fetchone()[0])
        return send(chat, "Pack yaratildi. Rasmlarni yuboring (jpg, png, webp). Tugagach /done")
    if text and mode == "rename" and pid:
        db.execute("UPDATE packs SET title=? WHERE id=? AND owner=?", (text[:64], pid, uid))
        db.commit()
        set_state(uid)
        return pack_card(chat, pid, uid)
    menu(chat)


def list_packs(chat, uid):
    rows = db.execute("SELECT id, title FROM packs WHERE owner=?", (uid,)).fetchall()
    if not rows:
        return send(chat, "Sizda hali pack yo'q.", kb([btn("➕ Yangi pack", "new")]))
    send(chat, "Packlaringiz:", {"inline_keyboard": [[btn(f"📦 {t}", f"open:{i}")] for i, t in rows[:50]]})


def is_owner(uid, pid):
    p = get_pack(pid)
    return bool(p and p[1] == uid)


def on_callback(cq):
    uid, chat = cq["from"]["id"], cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    action, _, arg = cq["data"].partition(":")
    api("answerCallbackQuery", callback_query_id=cq["id"])
    n = int(arg) if arg.isdigit() else None

    if action == "new":
        set_state(uid, "title")
        send(chat, "Pack nomini yozing:")
    elif action == "list":
        list_packs(chat, uid)
    elif action == "open" and n:
        set_state(uid)
        pack_card(chat, n, uid)
    elif action == "view" and n:
        show_items(chat, n, uid)
    elif action == "add" and n and is_owner(uid, n):
        set_state(uid, "adding", n)
        send(chat, "Rasmlarni yuboring. Tugagach /done")
    elif action == "ren" and n and is_owner(uid, n):
        set_state(uid, "rename", n)
        send(chat, "Yangi nomni yozing:")
    elif action == "delp" and n and is_owner(uid, n):
        api("editMessageText", chat_id=chat, message_id=mid, text="Packni o'chirasizmi?",
            reply_markup=kb([btn("Ha", f"delpy:{n}"), btn("Yo'q", f"open:{n}")]))
    elif action == "delpy" and n and is_owner(uid, n):
        db.execute("DELETE FROM items WHERE pack_id=?", (n,))
        db.execute("DELETE FROM packs WHERE id=?", (n,))
        db.commit()
        api("editMessageText", chat_id=chat, message_id=mid, text="🗑 Pack o'chirildi.")
    elif action == "deli" and n:
        row = db.execute("SELECT pack_id FROM items WHERE id=?", (n,)).fetchone()
        if row and is_owner(uid, row[0]):
            db.execute("DELETE FROM items WHERE id=?", (n,))
            db.commit()
            api("deleteMessage", chat_id=chat, message_id=mid)


class Ping(BaseHTTPRequestHandler):
    """Render Web Service port ochiq bo'lishini talab qiladi; UptimeRobot shu yerga so'rov yuboradi."""

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def run_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Ping).serve_forever()


def main():
    global BOT_USER
    threading.Thread(target=run_server, daemon=True).start()
    me = api("getMe")
    BOT_USER = me["username"]
    api("deleteWebhook")  # getUpdates webhook bilan ishlamaydi (409)
    api("setMyCommands", commands=[
        {"command": "start", "description": "Boshlash"},
        {"command": "newpack", "description": "Yangi pack"},
        {"command": "mypacks", "description": "Packlarim"},
        {"command": "done", "description": "Tugatish"},
    ])
    print("Bot ishga tushdi:", BOT_USER)
    offset = 0
    while True:
        updates = api("getUpdates", offset=offset, timeout=25) or []
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    on_message(u["message"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
            except Exception as e:
                print("handler error:", e)
        if not updates:
            time.sleep(1)


if __name__ == "__main__":
    main()
