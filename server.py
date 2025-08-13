import os
import re
import asyncio
import json
import hashlib
import websockets

USERS_FILE = "users.json"

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {u["email"]: u for u in data.get("users", [])}
    except FileNotFoundError:
        # konto testowe
        return {
            "test@example.com": {
                "email": "test@example.com",
                "password_sha256": "937e8d5fbb48bd4949536cd65b8d35c426b80d2f830c5c308e2cdec422ae2244",
                "display_name": "Tester"
            }
        }
    except Exception as e:
        print("Failed to load users:", e, flush=True)
        return {}

def save_users(users):
    data = {"users": list(users.values())}
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_valid_display_name(name: str) -> bool:
    # 3–20 znaków, litery/cyfry/_ i bez spacji
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

USERS = load_users()      # email -> {email, password_sha256, display_name}
CLIENTS = {}              # ws -> {"id": int, "email": str}
ACTIVE_EMAILS = set()     # zalogowane e-maile (blokada multi-login)
PLAYERS = {}              # id -> {"email": str, "name": str, "pos":[x,y,z], "rot":[x,y,z]}
NEXT_ID = 1

async def send(ws, obj): await ws.send(json.dumps(obj))

async def broadcast(obj, exclude=None):
    data = json.dumps(obj)
    for c in list(CLIENTS.keys()):
        if exclude is not None and c is exclude: continue
        try:    await c.send(data)
        except: pass

async def handle(ws):
    global NEXT_ID, USERS
    print("client connected", flush=True)

    authed_id = None
    authed_email = None
    try:
        async for msg in ws:
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                await send(ws, {"type":"error","code":"bad_json","message":"Invalid JSON"})
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
                    await send(ws, {"type":"error","code":"bad_credentials","message":"Błędny e-mail lub hasło"})
                    continue

                if email in ACTIVE_EMAILS:
                    print("login rejected (already online):", email, flush=True)
                    await send(ws, {"type":"error","code":"already_online","message":"Użytkownik jest już zalogowany gdzie indziej"})
                    continue

                pid = NEXT_ID; NEXT_ID += 1
                CLIENTS[ws] = {"id": pid, "email": email}
                ACTIVE_EMAILS.add(email)
                name = u.get("display_name") or email.split("@")[0]
                PLAYERS[pid] = {"email": email, "name": name, "pos":[0,0,0], "rot":[0,0,0]}
                authed_id = pid; authed_email = email

                print("login ok:", email, "pid", pid, flush=True)

                await send(ws, {
                    "type":"welcome","id":pid,
                    "self":{"email": email, "name": name},
                    "players":[{"id":i,"name":p["name"],"pos":p["pos"],"rot":p["rot"]}
                               for i,p in PLAYERS.items() if i!=pid]
                })
                await broadcast({"type":"spawn","id":pid,"name":name}, exclude=ws)
                continue

            # ---------- STATE ----------
            if t == "state" and authed_id:
                pos = data.get("pos",[0,0,0]); rot = data.get("rot",[0,0,0])
                PLAYERS[authed_id]["pos"] = pos
                PLAYERS[authed_id]["rot"] = rot
                await broadcast({"type":"state","id":authed_id,"pos":pos,"rot":rot}, exclude=ws)
                continue

            # ---------- SET NAME (po zalogowaniu) ----------
            if t == "set_name" and authed_id:
                name = (data.get("name") or "").strip()
                if not is_valid_display_name(name):
                    await send(ws, {"type":"error","code":"invalid_name","message":"Nazwa: 3–20, litery/cyfry/_"})
                    continue
                if is_name_taken(name, email_owner=authed_email):
                    await send(ws, {"type":"error","code":"name_taken","message":"Nazwa zajęta"})
                    continue
                PLAYERS[authed_id]["name"] = name
                if authed_email in USERS:
                    USERS[authed_email]["display_name"] = name
                    save_users(USERS)
                await broadcast({"type":"rename","id":authed_id,"name":name})
                print("rename:", authed_email, "->", name, flush=True)
                continue

            # ---------- REGISTER ----------
            if t == "register":
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                display_name = (data.get("displayName") or "").strip()

                print("register attempt:", email, display_name, flush=True)

                if not email or not password:
                    await send(ws, {"type":"error","code":"missing_fields","message":"email i hasło są wymagane"})
                    continue
                if email in USERS:
                    await send(ws, {"type":"error","code":"exists","message":"Użytkownik już istnieje"})
                    continue
                if not is_valid_display_name(display_name):
                    await send(ws, {"type":"error","code":"invalid_name","message":"Nazwa: 3–20, litery/cyfry/_"})
                    continue
                if is_name_taken(display_name):
                    await send(ws, {"type":"error","code":"name_taken","message":"Nazwa zajęta"})
                    continue

                USERS[email] = {
                    "email": email,
                    "password_sha256": sha256(password),
                    "display_name": display_name
                }
                save_users(USERS)
                await send(ws, {"type":"registered"})
                print("registered:", email, "name:", display_name, flush=True)
                continue

            # ---------- UNKNOWN ----------
            print("unknown message:", data, flush=True)
            await send(ws, {"type":"error","code":"unknown_type","message": f"Nieznany typ: {t}"})

    finally:
        info = CLIENTS.pop(ws, None)
        if info:
            pid = info["id"]; email = info["email"]
            ACTIVE_EMAILS.discard(email)
            PLAYERS.pop(pid, None)
            print("client disconnected:", email, "pid", pid, flush=True)
            await broadcast({"type":"despawn","id":pid})

async def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"WS server on 0.0.0.0:{port}", flush=True)
    async with websockets.serve(handle, "0.0.0.0", port, ping_interval=20, ping_timeout=20):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
