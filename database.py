# database.py
import os
import json
import asyncio
import pymongo
from concurrent.futures import ThreadPoolExecutor

DATA_FILE = "events_data.json"

MONGO_URI = os.environ.get("MONGO_URI")
client = pymongo.MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = client["ctw_bot_db"] if client is not None else None
db_col = mongo_db["bot_data"] if mongo_db is not None else None

def load_db():
    if db_col is not None:
        try:
            doc = db_col.find_one({"_id": "main"})
            if doc: 
                return json.loads(doc["json_data"])
        except Exception as e:
            raise RuntimeError(f"Mongo load failed, refusing to start so data isn't overwritten: {e}")

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)

    return {"server_config": {}, "events": {}, "blacklists": {}, "hosts": {}}

_db_executor = ThreadPoolExecutor(max_workers=1)

def _write_db(payload):
    if db_col is not None:
        try:
            db_col.replace_one(
                {"_id": "main"}, 
                {"_id": "main", "json_data": payload}, 
                upsert=True
            )
        except Exception as e:
            print("db save error:", e)
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("file save error:", e)

def save_db(data):
    payload = json.dumps(data)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _write_db(payload)
        return
    _db_executor.submit(_write_db, payload)

db = load_db()

# Initialize defaults
for k in ["server_config", "events", "blacklists", "hosts", "achievement_progress", "trials", "trial_hosts"]:
    if k not in db:
        db[k] = {}
if not isinstance(db.get("disabled_guilds"), list):
    db["disabled_guilds"] = []
if not isinstance(db.get("trusted_users"), dict):
    db["trusted_users"] = {}

def get_config(guild_id):
    gid = str(guild_id)
    if gid not in db["server_config"]:
        db["server_config"][gid] = {}
        save_db(db)
    return db["server_config"][gid]
