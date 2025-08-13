import os, asyncio, json, hashlib, websockets

USERS_FILE = "users.json"

def sha256(s:str)->str: return hashlib.sha256(s.encode()).hexdigest()

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return {u["email"]: u for u in json.load(f)["users"]}
    except Exception:
        # konto testowe: test@example.com / test1234
        return {
            "test@example.com": {
                "email": "test@example.com",
                "password_sha256": "937e8d5fbb48bd4949536cd65b8d35c426b80d2f830c5c308e2cdec422ae2244",
                "display_name": "Tester"
            }
        }

def save_users(users):
    data = {"users": list(users.values())}
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

USERS = load_users()     # email -> user
CLIENTS = {}             # ws -> {"id": int, "email": str}
ACTIVE_EMAILS = set()    # zablokuj wielokrotne logowanie
PLAYERS = {}             # id -> {"email": str, "name": str, "pos":[x,y,z], "rot":[x,y,z]}
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
    authed_id = None
    authed_email = None
    try:
        async for msg in ws:
            data = json.loads(msg)
            t = data.get("type")

            if t == "login":
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                u = USERS.get(email)
                if not u or sha256(password) != u["password_sha256"]:
                    await send(ws, {"type":"error","code":"bad_credentials","message":"Błędny e-mail lub hasło"})
                    continue
                if email in ACTIVE_EMAILS:
                    # <-- BLOKADA wielokrotnego logowania
                    await send(ws, {"type":"error","code":"already_online","message":"Użytkownik jest już zalogowany na innym urządzeniu"})
                    #
