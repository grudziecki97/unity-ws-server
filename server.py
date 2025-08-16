import os
import re
import time
import json
import asyncio
import hashlib
import websockets

# ---------- prosta "baza" użytkowników ----------
USERS_FILE = "users.json"

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {u["email"]: u for u in data.get("users", [])}
    except FileNotFoundError:
        # konto testowe: test@example.com / test1234
        return {
            "test@example.com": {
                "email": "test@example.com",
                "password_sha256": sha256("test1234"),
                "display_name": "Tester"
            }
        }

def save_users(users):
    data = {"users": list(users.values())}
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_valid_display_name(name: str) -> bool:
    # 3–20 znaków, litery/cyfry/_ bez spacji
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,20}", name or ""))

def is_name_taken(name: str, email_owner: str | None = None) -> bool:
    if not name:
        return False
    low = name.lower()
    for em, u in USERS.items():
        if email_owner and em == email_owner:
            continue
        if (u.get("display_name") or "").lower() == low:
            return True
    return False

USERS = load_users()                # email -> {email,password_sha256,display_name}
CLIENTS = {}                        # ws -> {"id": int, "email": str}
ACTIVE_EMAILS = set()               # blokada multi-logowania
PLAYERS = {}                        # id -> {"email","name","pos","rot"}
NEXT_ID = 1

# ---------- trwały zapis ostatnich pozycji ----------
SAVE_PATH = "lastpos.json"          # email -> {"pos":[x,y,z], "rot":[x,y,z]}
LAST_POS: dict[str, dict] = {}
_last_flush = 0.0

def load_lastpos():
    global LAST_POS
    try:
        if os.path.exists(SAVE_PATH):
            with open(SAVE_PATH, "r", encoding="utf-8") as f:
                LAST_POS = json.load(f)
    except Exception as e:
        print("load_lastpos error:", e, flush=True)

def save_lastpos_atomic():
    try:
        tmp = SAVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(LAST_POS, f, ensure_ascii=False)
        os.replace(tmp, SAVE_PATH)  # atomowa podmiana
    except Exception as e:
        print("save_lastpos error:", e, flush=True)

async def autosave_loop():
    while True:
        await asyncio.sleep(5)      # co 5 s spłucz do pliku
        save_lastpos_atomic()

load_lastpos()

# ---------- helpers ----------
async def send(ws, obj):
    await ws.send(json.dumps(obj, ensure_ascii=False))

async def broadcast(obj, exclude=None):
    data = json.dumps(obj, ensure_ascii=False)
    for c in list(CLIENTS.keys()):
        if exclude is not None and c is exclude:
            continue
        try:
            await c.send(data)
        except:
            pass

# ---------- główna obsługa klienta ----------
async def handle(ws):
    global NEXT_ID, USERS

    print("client connected", flush=True)
    authed_id = None
    authed_email = None

    try:
        async for msg in ws:
            # parsowanie JSON
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                await send(ws, {"type": "error", "code": "bad_json", "message": "Invalid JSON"})
                continue

            t = data.get("type")

            # ---------- LOGIN ----------
            if t == "login":
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""

                print("login attempt:", email, flush=True)

                u = USERS.get(email)
                if not u or sha256(password) != u["password_sha256"]:
                    print("login failed:", email, flush=True)
                    await send(ws, {"type": "error", "code": "bad_credentials", "message": "Błędny e-mail lub hasło"})
                    continue

                if email in ACTIVE_EMAILS:
                    print("login rejected (already online):", email, flush=True)
                    await send(ws, {"type": "error", "code": "already_online", "message": "Użytkownik jest już zalogowany gdzie indziej"})
                    continue

                pid = NEXT_ID
                NEXT_ID += 1
                CLIENTS[ws] = {"id": pid, "email": email}
                ACTIVE_EMAILS.add(email)

                name = u.get("display_name") or email.split("@")[0]

                # startowa pozycja z pliku lastpos (albo 0,0,0)
                start = LAST_POS.get(email, {"pos": [0, 0, 0], "rot": [0, 0, 0]})
                PLAYERS[pid] = {"email": email, "name": name, "pos": start["pos"], "rot": start["rot"]}

                authed_id = pid
                authed_email = email

                print("login ok:", email, "pid", pid, flush=True)

                await send(ws, {
                    "type": "welcome",
                    "id": pid,
                    "self": {"email": email, "name": name, "pos": start["pos"], "rot": start["rot"]},
                    "players": [
                        {"id": i, "name": p["name"], "pos": p["pos"], "rot": p["rot"]}
                        for i, p in PLAYERS.items() if i != pid
                    ]
                })

                await broadcast({"type": "spawn", "id": pid, "name": name}, exclude=ws)
                continue

            # ---------- STATE ----------
            if t == "state" and authed_id:
                pos = data.get("pos", [0, 0, 0])
                rot = data.get("rot", [0, 0, 0])

                # zaktualizuj w pamięci
                PLAYERS[authed_id]["pos"] = pos
                PLAYERS[authed_id]["rot"] = rot

                # zapamiętaj ostatnią pozycję gracza (po e-mailu)
                if authed_email:
                    LAST_POS[authed_email] = {"pos": pos, "rot": rot}
                # autosave_loop zapisze do pliku co 5 s

                await broadcast({"type": "state", "id": authed_id, "pos": pos, "rot": rot}, exclude=ws)
                continue

            # ---------- SAVE (opcjonalny, klientowy "flush") ----------
            if t == "save" and authed_email:
                pos = data.get("pos", [0, 0, 0])
                rot = data.get("rot", [0, 0, 0])
                LAST_POS[authed_email] = {"pos": pos, "rot": rot}
                save_lastpos_atomic()  # natychmiastowy zapis
                continue

            # ---------- SET NAME ----------
            if t == "set_name" and authed_id:
                name = (data.get("name") or "").strip()
                if not is_valid_display_name(name):
                    await send(ws, {"type": "error", "code": "invalid_name", "message": "Nazwa: 3–20, litery/cyfry/_"})
                    continue
                if is_name_taken(name, email_owner=authed_email):
                    await send(ws, {"type": "error", "code": "name_taken", "message": "Nazwa zajęta"})
                    continue

                PLAYERS[authed_id]["name"] = name
                if authed_email in USERS:
                    USERS[authed_email]["display_name"] = name
                    save_users(USERS)
                await broadcast({"type": "rename", "id": authed_id, "name": name})
                print("rename:", authed_email, "->", name, flush=True)
                continue

            # ---------- REGISTER ----------
            if t == "register":
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                display_name = (data.get("displayName") or "").strip() or email.split("@")[0]

                print("register attempt:", email, display_name, flush=True)

                if not email or not password:
                    await send(ws, {"type": "error", "code": "missing_fields", "message": "email i hasło są wymagane"})
                    continue
                if email in USERS:
                    await send(ws, {"type": "error", "code": "exists", "message": "Użytkownik już istnieje"})
                    continue
                if not is_valid_display_name(display_name):
                    await send(ws, {"type": "error", "code": "invalid_name", "message": "Nazwa: 3–20, litery/cyfry/_"})
                    continue
                if is_name_taken(display_name):
                    await send(ws, {"type": "error", "code": "name_taken", "message": "Nazwa zajęta"})
                    continue

                USERS[email] = {
                    "email": email,
                    "password_sha256": sha256(password),
                    "display_name": display_name[:20]
                }
                save_users(USERS)
                await send(ws, {"type": "registered"})
                print("registered:", email, "name:", display_name, flush=True)
                continue

            # ---------- UNKNOWN ----------
            print("unknown message:", data, flush=True)
            await send(ws, {"type": "error", "code": "unknown_type", "message": f"Nieznany typ: {t}"})

    finally:
        # Sprzątanie po rozłączeniu
        info = CLIENTS.pop(ws, None)
        if info:
            pid = info["id"]
            email = info["email"]
            ACTIVE_EMAILS.discard(email)
            PLAYERS.pop(pid, None)
            print("client disconnected:", email, "pid", pid, flush=True)
            await broadcast({"type": "despawn", "id": pid})
            # szybki flush pozycji do pliku (gdyby coś jeszcze nie spłynęło)
            save_lastpos_atomic()

# ---------- start serwera ----------
async def main():
    port = int(os.environ.get("PORT", "8080"))  # Koyeb/Render podają PORT w env
    print(f"WS server on 0.0.0.0:{port}", flush=True)

    async with websockets.serve(handle, "0.0.0.0", port, ping_interval=20, ping_timeout=20):
        # uruchom autosave w tle
        asyncio.create_task(autosave_loop())
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
