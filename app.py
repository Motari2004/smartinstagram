"""
Bluesky AI Vault — simplified service
AI chat: Bluesky fetch → vault → Instagram schedule / post-now (Zernio only)
"""

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from atproto import Client
import json
import os
import requests
from datetime import datetime, timedelta
import traceback
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import psycopg2
from psycopg2.extras import Json
import uuid
import re
import random
import time
import base64
import pytz
import threading
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor
load_dotenv()






# ============================================================
# SESSION STORAGE
# ============================================================

sessions = {}  # in-memory session cache







app = Flask(__name__, static_folder='static')
CORS(app)

# ============================================================
# CONFIG
# ============================================================

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://neondb_owner:npg_ntBDZ4XvJrC3@ep-shiny-sound-aypt72ka-pooler.c-5.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require'
)

ZERNIO_API_KEY = os.environ.get('ZERNIO_API_KEY')
ZERNIO_BASE_URL = "https://zernio.com/api/v1"
SCHEDULE_TIMEZONE = "Africa/Nairobi"











# ============================================================
# GEMINI CONFIG - Models with fallback
# ============================================================

# Google Gemini API — Load from environment variables only
_env_keys = os.environ.get('GEMINI_API_KEYS', '') or os.environ.get('GEMINI_API_KEY', '')
if _env_keys:
    GEMINI_API_KEYS = [k.strip() for k in _env_keys.split(',') if k.strip()]
    print(f"✅ Loaded {len(GEMINI_API_KEYS)} Gemini keys from environment")
else:
    GEMINI_API_KEYS = []
    print("⚠️  No GEMINI_API_KEYS environment variable set!")






# Models in order of preference (highest quality first)
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",   
    "gemini-2.5-flash-lite",   
    "gemini-3.6-flash",    
    "gemini-3.7-flash",        
    
]




GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# ============================================================
# GEMINI STATE VARIABLES
# ============================================================

_gemini_model_index = 0
_gemini_key_index = 0
_gemini_key_cooldown = {}
_gemini_model_cooldown = {}

# ============================================================
# GEMINI HELPER FUNCTIONS
# ============================================================

def next_gemini_key():
    """Get next available Gemini API key (skip cooldown keys)"""
    global _gemini_key_index
    if not GEMINI_API_KEYS:
        return None
    
    # Try to find a working key
    for _ in range(len(GEMINI_API_KEYS) * 2):
        key_index = _gemini_key_index % len(GEMINI_API_KEYS)
        key = GEMINI_API_KEYS[key_index]
        
        # Check if key is on cooldown
        if key in _gemini_key_cooldown:
            cooldown_until = _gemini_key_cooldown[key]
            if datetime.now() < cooldown_until:
                _gemini_key_index += 1
                continue
        
        _gemini_key_index += 1
        return key
    
    # All keys on cooldown
    print("⚠️ All API keys on cooldown")
    return GEMINI_API_KEYS[0] if GEMINI_API_KEYS else None

def next_gemini_model():
    """Get next model in round-robin fashion"""
    global _gemini_model_index
    if not GEMINI_MODELS:
        return "gemini-2.5-flash-lite"
    
    model = GEMINI_MODELS[_gemini_model_index % len(GEMINI_MODELS)]
    _gemini_model_index += 1
    return model

def handle_model_rate_limit(model):
    """Put a model on cooldown if it's rate-limited"""
    _gemini_model_cooldown[model] = datetime.now() + timedelta(seconds=60)
    print(f"⏳ Model {model} on cooldown for 60 seconds")





def call_gemini(messages, tools=None, model=None, max_tokens=800, timeout=35):
    """Call Gemini API with automatic model fallback on errors"""
    
    # If no model specified, get next model
    if model is None:
        model = next_gemini_model()
    
    # Check if model is on cooldown
    if model in _gemini_model_cooldown:
        cooldown_until = _gemini_model_cooldown[model]
        if datetime.now() < cooldown_until:
            print(f"⏳ Model {model} on cooldown, trying next model...")
            next_model = next_gemini_model()
            if next_model != model:
                return call_gemini(messages, tools, next_model, max_tokens, timeout)
            return None, f"All models on cooldown"
    
    # Get API key
    key = next_gemini_key()
    if not key:
        return None, "No Gemini API keys. Set GEMINI_API_KEYS in environment."
    
    print(f"🔑 Using Gemini key: {key[:12]}... with model: {model}")
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    
    try:
        r = requests.post(
            f"{GEMINI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout
        )
        print(f"📥 Gemini response status: {r.status_code} (model: {model})")
        
        # Handle 403 - API key leaked/invalid - try next key
        if r.status_code == 403:
            print(f"❌ API key is invalid or leaked! Status: 403")
            if key in _gemini_key_cooldown:
                _gemini_key_cooldown[key] = datetime.now() + timedelta(seconds=300)
            else:
                _gemini_key_cooldown[key] = datetime.now() + timedelta(seconds=300)
            print(f"⏳ Key {key[:12]}... on cooldown for 5 minutes")
            
            next_key = next_gemini_key()
            if next_key and next_key != key:
                print(f"🔄 Switching to next API key")
                return call_gemini(messages, tools, model, max_tokens, timeout)
            return None, f"All API keys invalid or on cooldown - please check your GEMINI_API_KEYS"
        
        # Handle rate limit - try next model
        if r.status_code == 429:
            print(f"⚠️ Rate limit hit for model {model}")
            handle_model_rate_limit(model)
            
            next_model = next_gemini_model()
            if next_model != model:
                print(f"🔄 Switching to next model: {next_model}")
                return call_gemini(messages, tools, next_model, max_tokens, timeout)
            return None, f"Rate limit exceeded - all models exhausted"
        
        # Handle other errors - try next model
        if r.status_code != 200:
            print(f"❌ Gemini error with model {model}: {r.text[:200]}")
            
            if r.status_code != 400 and r.status_code != 403:
                next_model = next_gemini_model()
                if next_model != model:
                    print(f"🔄 Switching to next model: {next_model}")
                    return call_gemini(messages, tools, next_model, max_tokens, timeout)
            
            return None, f"Gemini {r.status_code} with {model}: {r.text[:300]}"
        
        # Success! Reset model cooldown
        if model in _gemini_model_cooldown:
            del _gemini_model_cooldown[model]
        
        return r.json(), None
        
    except Exception as e:
        print(f"❌ Gemini exception with {model}: {e}")
        next_model = next_gemini_model()
        if next_model != model:
            print(f"🔄 Switching to next model on exception: {next_model}")
            return call_gemini(messages, tools, next_model, max_tokens, timeout)
        return None, str(e)
















# ============================================================
# MULTI-ZERNIO-API CONFIG
# ============================================================
# Keys are always read from .env (via load_dotenv). Supported forms:
#   ZERNIO_API_KEY1=sk_xxx / ZERNIO_API_KEY2=sk_yyy   (preferred)
#   ZERNIO_API_KEYS=sk_xxx,sk_yyy                     (comma-separated)
#   ZERNIO_API_KEY=sk_xxx                             (legacy single)
# Accounts for each key are auto-detected by calling Zernio /accounts.

def _detect_accounts_for_key(api_key, label="key"):
    """Query Zernio for accounts belonging to this API key."""
    accounts = []
    if not api_key:
        return accounts
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.get(
            f"{ZERNIO_BASE_URL}/accounts",
            headers=headers,
            timeout=15
        )
        if response.status_code == 200:
            zernio_accounts = response.json().get('accounts', [])
            for acc in zernio_accounts:
                username = acc.get('username')
                if username:
                    accounts.append({
                        'username': username,
                        'platform': acc.get('platform', 'instagram'),
                        'display_name': acc.get('displayName', ''),
                        'account_id': acc.get('_id')
                    })
            print(f"✅ Auto-detected {len(accounts)} accounts for {label}")
        else:
            print(f"⚠️ Could not fetch accounts for {label}: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Error fetching accounts for {label}: {e}")
    return accounts


def get_zernio_api_keys():
    """
    Get all Zernio API keys from .env and auto-detect their accounts.
    Prefer numbered keys, then ZERNIO_API_KEYS (csv), then legacy ZERNIO_API_KEY.
    """
    load_dotenv(override=False)

    keys = []
    seen = set()

    # 1) Numbered: ZERNIO_API_KEY1, ZERNIO_API_KEY2, …
    i = 1
    while True:
        key = (os.environ.get(f'ZERNIO_API_KEY{i}') or '').strip()
        if not key:
            break
        if key not in seen:
            env_var = f'ZERNIO_API_KEY{i}'
            accounts = _detect_accounts_for_key(key, env_var)
            keys.append({
                'key': key,
                'index': i,
                'accounts': accounts,
                'env_var': env_var,
                'account_count': len(accounts)
            })
            seen.add(key)
        i += 1

    # 2) Comma-separated: ZERNIO_API_KEYS=sk_a,sk_b
    csv = (os.environ.get('ZERNIO_API_KEYS') or '').strip()
    if csv:
        for part in csv.split(','):
            key = part.strip()
            if not key or key in seen:
                continue
            idx = len(keys) + 1
            env_var = 'ZERNIO_API_KEYS'
            accounts = _detect_accounts_for_key(key, f"{env_var}[{idx}]")
            keys.append({
                'key': key,
                'index': idx,
                'accounts': accounts,
                'env_var': env_var,
                'account_count': len(accounts)
            })
            seen.add(key)

    # 3) Legacy single key
    if not keys:
        default_key = (os.environ.get('ZERNIO_API_KEY') or '').strip()
        if default_key:
            accounts = _detect_accounts_for_key(default_key, 'ZERNIO_API_KEY')
            keys.append({
                'key': default_key,
                'index': 1,
                'accounts': accounts,
                'env_var': 'ZERNIO_API_KEY',
                'account_count': len(accounts)
            })

    return keys


def ensure_zernio_keys_loaded(for_auto: bool = False) -> dict:
    """
    Explicitly re-check .env for Zernio keys (used by autonomous pipelines).
    Returns {success, count, keys_preview, message, keys}.
    """
    load_dotenv(override=False)
    keys = get_zernio_api_keys()

    global ZERNIO_API_KEY
    if keys:
        ZERNIO_API_KEY = keys[0]['key']

    previews = []
    for k in keys:
        prev = (k['key'][:12] + '…') if len(k.get('key') or '') > 12 else (k.get('key') or '?')
        acc_names = [a.get('username') for a in (k.get('accounts') or []) if a.get('username')]
        acc = ', '.join(acc_names) if acc_names else 'auto-detect'
        previews.append(f"{k.get('env_var', k.get('index'))}: {prev} ({acc})")

    if keys:
        msg = f"Loaded {len(keys)} Zernio API key(s) from .env"
        print(f"🔑 {msg}")
        for line in previews:
            print(f"   • {line}")
    else:
        msg = "No Zernio API keys found in .env. Set ZERNIO_API_KEY / ZERNIO_API_KEY1 / ZERNIO_API_KEYS."
        if for_auto:
            msg = "⚠️ Auto pilot cannot post: " + msg
        print(f"⚠️ {msg}")

    return {
        "success": len(keys) > 0,
        "count": len(keys),
        "keys_preview": previews,
        "message": msg,
        "keys": keys,
    }


def tool_check_zernio_key(api_key: str = None, save_to_db: bool = True) -> dict:
    """
    Check a specific Zernio API key the user provides (not only from .env).
    Fetches all accounts on that key from Zernio and optionally saves them to DB.
    """
    if not api_key or not str(api_key).strip():
        return {
            "success": False,
            "error": "No API key provided",
            "message": "Paste a Zernio key like: check key sk_xxxxx"
        }

    raw = str(api_key).strip()
    # Allow "ZERNIO_API_KEY2=sk_..." or bare sk_...
    if '=' in raw:
        raw = raw.split('=', 1)[1].strip()
    raw = raw.strip().strip('"').strip("'")

    if not raw.startswith('sk_') and len(raw) < 20:
        return {
            "success": False,
            "error": "That does not look like a Zernio API key",
            "message": "Zernio keys usually start with sk_ — paste the full key."
        }

    headers = get_zernio_headers_for_key(raw)
    try:
        response = requests.get(
            f"{ZERNIO_BASE_URL}/accounts",
            headers=headers,
            timeout=15
        )
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"❌ Could not reach Zernio: {e}"
        }

    if response.status_code == 401:
        return {
            "success": False,
            "error": "Invalid API key",
            "message": "❌ This Zernio API key is invalid or revoked (401)."
        }
    if response.status_code == 429:
        return {
            "success": False,
            "error": "Rate limited",
            "message": "⚠️ Zernio rate-limited this key. Try again in a minute."
        }
    if response.status_code != 200:
        return {
            "success": False,
            "error": f"HTTP {response.status_code}",
            "message": f"❌ Zernio error {response.status_code}: {response.text[:200]}"
        }

    zernio_accounts = response.json().get('accounts', []) or []
    accounts = []
    for acc in zernio_accounts:
        username = acc.get('username')
        if not username:
            continue
        entry = {
            "username": username,
            "display_name": acc.get('displayName') or username,
            "platform": acc.get('platform', 'instagram'),
            "account_id": acc.get('_id'),
            "profile_picture": acc.get('profilePicture'),
        }
        accounts.append(entry)

        if save_to_db:
            try:
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO zernio_accounts
                        (account_id, platform, display_name, username, profile_picture,
                         api_key, api_key_index, is_active, last_sync)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                        ON CONFLICT (account_id, platform) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            username = EXCLUDED.username,
                            profile_picture = EXCLUDED.profile_picture,
                            api_key = EXCLUDED.api_key,
                            is_active = TRUE,
                            last_sync = CURRENT_TIMESTAMP
                    """, (
                        entry['account_id'],
                        entry['platform'],
                        entry['display_name'],
                        entry['username'],
                        entry.get('profile_picture'),
                        raw,
                        None,  # unknown index when user pastes a free-form key
                    ))
                    conn.commit()
                    cur.close()
                    conn.close()
            except Exception as e:
                print(f"Save account {username}: {e}")

    key_preview = raw[:12] + '…' if len(raw) > 12 else raw
    if not accounts:
        msg = (
            f"✅ Key valid ({key_preview})\n"
            f"But no accounts are connected on this Zernio key yet.\n"
            f"Connect Instagram in the Zernio dashboard first."
        )
    else:
        lines = [f"✅ Key valid ({key_preview}) — {len(accounts)} account(s):"]
        for a in accounts:
            lines.append(
                f"  • @{a['username']} ({a['display_name']}) — "
                f"{a['platform']} — id={a['account_id']}"
            )
        msg = "\n".join(lines)

    return {
        "success": True,
        "valid": True,
        "key_preview": key_preview,
        "count": len(accounts),
        "accounts": accounts,
        "message": msg,
    }













def get_zernio_headers_for_key(api_key):
    """Get Zernio headers for a specific API key."""
    if not api_key:
        return {}
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }


def save_zernio_account_row(account_id, platform, display_name, username,
                            profile_picture, api_key, api_key_index=None):
    """
    Upsert one Zernio account. Prefer ON CONFLICT; if the unique constraint
    is missing (legacy DB), fall back to UPDATE then INSERT.
    """
    platform = platform or 'instagram'
    if not account_id:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO zernio_accounts
                (account_id, platform, display_name, username, profile_picture,
                 api_key, api_key_index, is_active, last_sync)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT (account_id, platform) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    username = EXCLUDED.username,
                    profile_picture = EXCLUDED.profile_picture,
                    api_key = EXCLUDED.api_key,
                    api_key_index = COALESCE(EXCLUDED.api_key_index, zernio_accounts.api_key_index),
                    is_active = TRUE,
                    last_sync = CURRENT_TIMESTAMP
            """, (
                account_id, platform, display_name, username,
                profile_picture, api_key, api_key_index
            ))
        except Exception:
            # Legacy DB without unique constraint
            cur.execute("""
                UPDATE zernio_accounts SET
                    display_name = %s,
                    username = %s,
                    profile_picture = %s,
                    api_key = %s,
                    api_key_index = COALESCE(%s, api_key_index),
                    is_active = TRUE,
                    last_sync = CURRENT_TIMESTAMP
                WHERE account_id = %s AND platform = %s
            """, (
                display_name, username, profile_picture,
                api_key, api_key_index, account_id, platform
            ))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO zernio_accounts
                    (account_id, platform, display_name, username, profile_picture,
                     api_key, api_key_index, is_active, last_sync)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                """, (
                    account_id, platform, display_name, username,
                    profile_picture, api_key, api_key_index
                ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Save error for account {username}: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False

def get_zernio_headers_for_account(account_username=None):
    """Get Zernio headers with the correct API key for the account."""
    if account_username:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT api_key FROM zernio_accounts 
                WHERE username = %s AND is_active = TRUE
                LIMIT 1
            """, (account_username,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return get_zernio_headers_for_key(row[0])
    
    # Fallback to first key
    keys = get_zernio_api_keys()
    if keys:
        return get_zernio_headers_for_key(keys[0].get('key'))
    
    # Ultimate fallback
    default_key = os.environ.get('ZERNIO_API_KEY')
    if default_key:
        return get_zernio_headers_for_key(default_key)
    
    return {}

def refresh_all_zernio_accounts():
    """Refresh all Zernio accounts from all API keys (re-reads .env)."""
    status = ensure_zernio_keys_loaded()
    keys = status.get('keys') or []
    all_accounts = []

    for key_info in keys:
        api_key = key_info.get('key')
        index = key_info.get('index')
        # Prefer accounts already fetched during ensure/get_zernio_api_keys
        prefetched = key_info.get('accounts') or []
        if prefetched:
            for acc in prefetched:
                # prefetched shape may be our normalized dict
                aid = acc.get('account_id') or acc.get('_id')
                uname = acc.get('username')
                save_zernio_account_row(
                    account_id=aid,
                    platform=acc.get('platform', 'instagram'),
                    display_name=acc.get('display_name') or acc.get('displayName') or uname,
                    username=uname,
                    profile_picture=acc.get('profile_picture') or acc.get('profilePicture'),
                    api_key=api_key,
                    api_key_index=index,
                )
                all_accounts.append(acc)
            continue

        headers = get_zernio_headers_for_key(api_key)
        if not headers:
            continue
        try:
            response = requests.get(
                f"{ZERNIO_BASE_URL}/accounts",
                headers=headers,
                timeout=15
            )
            if response.status_code == 200:
                accounts = response.json().get('accounts', [])
                for acc in accounts:
                    save_zernio_account_row(
                        account_id=acc.get('_id'),
                        platform=acc.get('platform', 'instagram'),
                        display_name=acc.get('displayName'),
                        username=acc.get('username'),
                        profile_picture=acc.get('profilePicture'),
                        api_key=api_key,
                        api_key_index=index,
                    )
                all_accounts.extend(accounts)
        except Exception as e:
            print(f"Error fetching accounts for key {index}: {e}")
    
    return all_accounts





















# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"❌ DB connection error: {e}")
        return None

















def init_db():
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL,
                handle TEXT NOT NULL,
                display_name TEXT,
                avatar TEXT,
                session_string TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_handle ON sessions(handle)')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS handlers (
                id SERIAL PRIMARY KEY,
                handle TEXT UNIQUE NOT NULL,
                display_name TEXT,
                avatar TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                selected BOOLEAN DEFAULT TRUE,
                is_default BOOLEAN DEFAULT FALSE
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS vault (
                id SERIAL PRIMARY KEY,
                uri TEXT UNIQUE NOT NULL,
                author TEXT NOT NULL,
                display_name TEXT,
                text TEXT,
                images JSONB,
                video JSONB,
                likes INTEGER DEFAULT 0,
                reposts INTEGER DEFAULT 0,
                replies INTEGER DEFAULT 0,
                created_at TIMESTAMP,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                handler_handle TEXT,
                notes TEXT
            )
        ''')
        try:
            cur.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS video JSONB")
            cur.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS notes TEXT")
        except Exception:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS deleted_posts (
                id SERIAL PRIMARY KEY,
                uri TEXT UNIQUE NOT NULL,
                handler_handle TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS posted_posts (
                id SERIAL PRIMARY KEY,
                vault_id INTEGER REFERENCES vault(id),
                uri TEXT NOT NULL,
                platform VARCHAR(50) NOT NULL,
                platform_post_id VARCHAR(200),
                status VARCHAR(50) DEFAULT 'pending',
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                metadata JSONB,
                UNIQUE(uri, platform)
            )
        ''')

        # ===== UPDATED: zernio_accounts with multi-platform support =====
        cur.execute('''
            CREATE TABLE IF NOT EXISTS zernio_accounts (
                id SERIAL PRIMARY KEY,
                account_id VARCHAR(100) NOT NULL,
                platform VARCHAR(50) NOT NULL DEFAULT 'instagram',
                display_name VARCHAR(200),
                username VARCHAR(100),
                profile_picture TEXT,
                api_key TEXT,
                api_key_index INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                last_sync TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, platform)
            )
        ''')
        # Migrate older DBs that created zernio_accounts without multi-key columns
        for col_sql in (
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS api_key TEXT",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS api_key_index INTEGER",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS profile_picture TEXT",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS last_sync TIMESTAMP",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS display_name VARCHAR(200)",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS username VARCHAR(100)",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS platform VARCHAR(50) DEFAULT 'instagram'",
            "ALTER TABLE zernio_accounts ADD COLUMN IF NOT EXISTS account_id VARCHAR(100)",
        ):
            try:
                cur.execute(col_sql)
            except Exception as mig_e:
                print(f"zernio_accounts migrate skip: {mig_e}")

        # UNIQUE(account_id, platform) required for ON CONFLICT upserts
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS zernio_accounts_account_id_platform_uidx
                ON zernio_accounts (account_id, platform)
            """)
        except Exception as idx_e:
            # Duplicates may block the index — remove older dupes then retry
            print(f"zernio_accounts unique index attempt: {idx_e}")
            try:
                cur.execute("""
                    DELETE FROM zernio_accounts a
                    USING zernio_accounts b
                    WHERE a.account_id IS NOT NULL
                      AND a.account_id = b.account_id
                      AND COALESCE(a.platform, 'instagram') = COALESCE(b.platform, 'instagram')
                      AND a.ctid < b.ctid
                """)
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS zernio_accounts_account_id_platform_uidx
                    ON zernio_accounts (account_id, platform)
                """)
            except Exception as idx_e2:
                print(f"zernio_accounts unique index failed: {idx_e2}")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                session_key TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS auto_config (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL DEFAULT 'default',
                enabled BOOLEAN DEFAULT FALSE,
                source_handle TEXT,
                account_id TEXT,
                account_username TEXT,
                content_type TEXT DEFAULT 'feed',
                poll_interval_sec INTEGER DEFAULT 300,
                media_only BOOLEAN DEFAULT TRUE,
                include_reposts BOOLEAN DEFAULT FALSE,
                max_posts_per_run INTEGER DEFAULT 2,
                bluesky_handle TEXT,
                bluesky_app_password TEXT,
                last_run_at TIMESTAMP,
                last_error TEXT,
                last_result TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS auto_seen (
                id SERIAL PRIMARY KEY,
                config_name TEXT NOT NULL DEFAULT 'default',
                uri TEXT NOT NULL,
                posted BOOLEAN DEFAULT FALSE,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(config_name, uri)
            )
        ''')

        # ===== ADDED: Bluesky accounts table for direct posting =====
        cur.execute('''
            CREATE TABLE IF NOT EXISTS bluesky_accounts (
                id SERIAL PRIMARY KEY,
                handle TEXT UNIQUE NOT NULL,
                display_name TEXT,
                avatar TEXT,
                session_string TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ===== ADDED: Platform mappings for auto_config =====
        cur.execute('''
            CREATE TABLE IF NOT EXISTS platform_mappings (
                id SERIAL PRIMARY KEY,
                config_name TEXT NOT NULL,
                platform VARCHAR(50) NOT NULL,
                account_username VARCHAR(100),
                account_id TEXT,
                UNIQUE(config_name, platform)
            )
        ''')

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized with multi-platform support")
    except Exception as e:
        print(f"❌ DB init error: {e}")
        traceback.print_exc()















init_db()

# ============================================================
# AUTO PILOT (background autonomy)
# ============================================================

_auto_thread = None
_auto_stop = threading.Event()


def _load_auto_config(name='default'):
    try:
        conn = get_db_connection()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute("SELECT * FROM auto_config WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return None
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()
        return dict(zip(cols, row))
    except Exception as e:
        print(f"load auto_config: {e}")
        return None


def _list_auto_configs():
    try:
        conn = get_db_connection()
        if not conn:
            return []
        cur = conn.cursor()
        cur.execute("SELECT * FROM auto_config ORDER BY name")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        print(f"list auto_configs: {e}")
        return []


def _save_auto_config(cfg: dict):
    try:
        conn = get_db_connection()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO auto_config (
                name, enabled, source_handle, account_id, account_username,
                content_type, poll_interval_sec, media_only, include_reposts,
                max_posts_per_run, bluesky_handle, bluesky_app_password,
                last_run_at, last_error, last_result, updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP
            )
            ON CONFLICT (name) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                source_handle = COALESCE(EXCLUDED.source_handle, auto_config.source_handle),
                account_id = COALESCE(EXCLUDED.account_id, auto_config.account_id),
                account_username = COALESCE(EXCLUDED.account_username, auto_config.account_username),
                content_type = COALESCE(EXCLUDED.content_type, auto_config.content_type),
                poll_interval_sec = COALESCE(EXCLUDED.poll_interval_sec, auto_config.poll_interval_sec),
                media_only = COALESCE(EXCLUDED.media_only, auto_config.media_only),
                include_reposts = COALESCE(EXCLUDED.include_reposts, auto_config.include_reposts),
                max_posts_per_run = COALESCE(EXCLUDED.max_posts_per_run, auto_config.max_posts_per_run),
                bluesky_handle = COALESCE(EXCLUDED.bluesky_handle, auto_config.bluesky_handle),
                bluesky_app_password = COALESCE(EXCLUDED.bluesky_app_password, auto_config.bluesky_app_password),
                last_run_at = COALESCE(EXCLUDED.last_run_at, auto_config.last_run_at),
                last_error = EXCLUDED.last_error,
                last_result = EXCLUDED.last_result,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            cfg.get('name', 'default'),
            bool(cfg.get('enabled', False)),
            cfg.get('source_handle'),
            cfg.get('account_id'),
            cfg.get('account_username'),
            cfg.get('content_type', 'feed'),
            int(cfg.get('poll_interval_sec') or 300),
            bool(cfg.get('media_only', True)),
            bool(cfg.get('include_reposts', False)),
            int(cfg.get('max_posts_per_run') or 2),
            cfg.get('bluesky_handle'),
            cfg.get('bluesky_app_password'),
            cfg.get('last_run_at'),
            cfg.get('last_error'),
            cfg.get('last_result'),
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"save auto_config: {e}")
        traceback.print_exc()
        return False


def _auto_seen(uri, config_name='default'):
    try:
        conn = get_db_connection()
        if not conn:
            return True  # fail safe: treat as seen
        cur = conn.cursor()
        cur.execute("SELECT id FROM auto_seen WHERE config_name=%s AND uri=%s", (config_name, uri))
        exists = cur.fetchone() is not None
        cur.close()
        conn.close()
        return exists
    except Exception:
        return True


def _auto_mark_seen(uri, posted=False, config_name='default'):
    try:
        conn = get_db_connection()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO auto_seen (config_name, uri, posted)
            VALUES (%s, %s, %s)
            ON CONFLICT (config_name, uri) DO UPDATE SET posted = EXCLUDED.posted
        ''', (config_name, uri, posted))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"auto_mark_seen: {e}")


def _get_bluesky_client_for_auto(cfg):
    """Prefer live session, else login with stored app password, else restore from DB."""
    # 1) any in-memory session
    for sid, s in sessions.items():
        if s.get('client'):
            return s['client'], sid

    # 2) restore latest DB session (prefer login handle, then any valid session)
    login_handle = cfg.get('bluesky_handle')
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            row = None
            if login_handle:
                cur.execute('''
                    SELECT session_id, session_string, handle FROM sessions
                    WHERE handle = %s AND expires_at > CURRENT_TIMESTAMP
                    ORDER BY last_used_at DESC LIMIT 1
                ''', (login_handle,))
                row = cur.fetchone()
            if not row:
                cur.execute('''
                    SELECT session_id, session_string, handle FROM sessions
                    WHERE expires_at > CURRENT_TIMESTAMP
                    ORDER BY last_used_at DESC LIMIT 1
                ''')
                row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                client = Client()
                client.login(session_string=row[1])
                sid = row[0]
                sessions[sid] = {
                    'client': client,
                    'handle': row[2],
                    'session_string': row[1]
                }
                print(f"✅ Auto restored Bluesky session for @{row[2]}")
                return client, sid
    except Exception as e:
        print(f"auto restore session: {e}")
        traceback.print_exc()

    # 3) login with stored credentials
    bsky_user = cfg.get('bluesky_handle')
    bsky_pass = cfg.get('bluesky_app_password')
    if bsky_user and bsky_pass:
        result = tool_login(bsky_user, bsky_pass)
        if result.get('success'):
            sid = result['session_id']
            return sessions[sid]['client'], sid

    return None, None


def run_auto_once(name='default'):
    """One autonomous cycle: fetch new posts → vault → post to Instagram."""
    # Always re-check .env for Zernio keys before posting
    key_status = ensure_zernio_keys_loaded(for_auto=True)
    if not key_status.get('success'):
        return {
            "success": False,
            "error": key_status.get('message') or "No Zernio API keys in .env",
            "keys_checked": True,
        }

    cfg = _load_auto_config(name)
    if not cfg:
        return {"success": False, "error": "No auto config. Set it up first."}
    if not cfg.get('enabled'):
        return {"success": False, "error": "Auto pilot is disabled", "skipped": True}

    source = cfg.get('source_handle')
    if not source:
        return {"success": False, "error": "source_handle not set"}

    client, session_id = _get_bluesky_client_for_auto(cfg)
    if not client or not session_id:
        msg = "No Bluesky session. Login once in chat, or set bluesky_handle + app password in auto config."
        _save_auto_config({**cfg, 'last_error': msg, 'last_run_at': datetime.now()})
        return {"success": False, "error": msg}

    try:
        fetch = tool_fetch_posts(
            session_id=session_id,
            actor=source,
            limit=max(5, int(cfg.get('max_posts_per_run') or 2) * 3),
            include_reposts=bool(cfg.get('include_reposts')),
            media_only=bool(cfg.get('media_only', True))
        )
        if not fetch.get('success'):
            _save_auto_config({**cfg, 'last_error': fetch.get('error'), 'last_run_at': datetime.now()})
            return fetch

        posts = fetch.get('posts') or []
        new_posts = []
        for p in posts:
            uri = p.get('uri')
            if not uri or _auto_seen(uri, name):
                continue
            new_posts.append(p)
            if len(new_posts) >= int(cfg.get('max_posts_per_run') or 2):
                break

        if not new_posts:
            result_msg = f"No new posts from @{source}"
            _save_auto_config({**cfg, 'last_error': None, 'last_result': result_msg, 'last_run_at': datetime.now()})
            return {"success": True, "posted_count": 0, "message": result_msg}

        # save to vault
        tool_add_to_vault(new_posts, handler_handle=source)

        account_id = resolve_instagram_account_id(cfg.get('account_id'), cfg.get('account_username'))
        account_username = cfg.get('account_username')
        content_type = cfg.get('content_type') or 'feed'
        if not account_id:
            msg = f"Bad Instagram account on pipeline {name}: {cfg.get('account_id')} / {account_username}"
            _save_auto_config({**cfg, 'last_error': msg, 'last_run_at': datetime.now()})
            return {"success": False, "error": msg}
        posted = 0
        errors = []
        for p in new_posts:
            r = tool_post_now(
                uri=p.get('uri'),
                content_type=content_type,
                account_id=account_id,
                account_username=account_username
            )
            _auto_mark_seen(p.get('uri'), posted=bool(r.get('success')), config_name=name)
            if r.get('success'):
                posted += 1
            else:
                errors.append(r.get('error'))
            time.sleep(1.5)

        result_msg = f"Auto: posted {posted}/{len(new_posts)} from @{source}"
        _save_auto_config({
            **cfg,
            'last_error': '; '.join(errors) if errors else None,
            'last_result': result_msg,
            'last_run_at': datetime.now()
        })
        print(f"🤖 {result_msg}")
        return {
            "success": True,
            "posted_count": posted,
            "fetched_new": len(new_posts),
            "errors": errors,
            "message": result_msg
        }
    except Exception as e:
        traceback.print_exc()
        _save_auto_config({**cfg, 'last_error': str(e), 'last_run_at': datetime.now()})
        return {"success": False, "error": str(e)}


def _auto_loop():
    print("🤖 Auto pilot loop started")
    while not _auto_stop.is_set():
        try:
            configs = [c for c in _list_auto_configs() if c.get('enabled')]
            if configs:
                intervals = []
                for cfg in configs:
                    name = cfg.get('name') or 'default'
                    print(f"🤖 Running pipeline: {name} (@{cfg.get('source_handle')})")
                    run_auto_once(name)
                    intervals.append(max(60, int(cfg.get('poll_interval_sec') or 300)))
                    if _auto_stop.is_set():
                        break
                interval = min(intervals) if intervals else 60
            else:
                interval = 30  # check often whether any got enabled
        except Exception as e:
            print(f"auto loop error: {e}")
            interval = 60
        _auto_stop.wait(interval)
    print("🤖 Auto pilot loop stopped")


def start_auto_pilot():
    global _auto_thread
    # Re-read Zernio keys from .env before starting autonomous posting
    key_status = ensure_zernio_keys_loaded(for_auto=True)
    if not key_status.get('success'):
        return {
            "success": False,
            "error": key_status.get('message') or "No Zernio API keys found in .env",
            "message": key_status.get('message'),
            "keys_checked": True,
        }
    if _auto_thread and _auto_thread.is_alive():
        return {
            "success": True,
            "message": f"Auto pilot already running · {key_status.get('count', 0)} Zernio key(s) from .env",
            "keys": key_status.get('keys_preview'),
        }
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    return {
        "success": True,
        "message": f"Auto pilot started · {key_status.get('count', 0)} Zernio key(s) loaded from .env",
        "keys": key_status.get('keys_preview'),
    }


def stop_auto_pilot():
    _auto_stop.set()
    return {"success": True, "message": "Auto pilot stop requested"}


def tool_auto_status(name: str = None) -> dict:
    """Status for one pipeline or all pipelines (includes .env Zernio key check)."""
    key_status = ensure_zernio_keys_loaded(for_auto=True)
    running = _auto_thread is not None and _auto_thread.is_alive()
    configs = _list_auto_configs()
    if name:
        configs = [c for c in configs if c.get('name') == name]
    pipelines = []
    for cfg in configs:
        pipelines.append({
            "name": cfg.get('name'),
            "enabled": bool(cfg.get('enabled')),
            "source_handle": cfg.get('source_handle'),
            "account_username": cfg.get('account_username') or cfg.get('account_id'),
            "poll_interval_sec": cfg.get('poll_interval_sec'),
            "max_posts_per_run": cfg.get('max_posts_per_run'),
            "content_type": cfg.get('content_type'),
            "last_run_at": str(cfg.get('last_run_at')) if cfg.get('last_run_at') else None,
            "last_result": cfg.get('last_result'),
            "last_error": cfg.get('last_error'),
        })
    any_enabled = any(p['enabled'] for p in pipelines)
    key_line = f"Zernio keys (.env): {key_status.get('count', 0)} loaded"
    if not key_status.get('success'):
        key_line = "Zernio keys (.env): ⚠️ NONE — posting will fail"
    lines = [
        f"Auto engine: {'RUNNING' if running else 'STOPPED'} · {len(pipelines)} pipeline(s)",
        key_line,
    ]
    for p in pipelines:
        state = 'ON' if p['enabled'] else 'OFF'
        lines.append(
            f"• [{p['name']}] {state} · @{p.get('source_handle') or '?'} → {p.get('account_username') or '?'} · "
            f"every {p.get('poll_interval_sec') or 300}s · last: {p.get('last_result') or 'never'}"
        )
    return {
        "success": True,
        "running": running,
        "enabled": any_enabled,
        "pipeline_count": len(pipelines),
        "pipelines": pipelines,
        "zernio_keys_count": key_status.get('count', 0),
        "zernio_keys_ok": bool(key_status.get('success')),
        "source_handle": (next((p['source_handle'] for p in pipelines if p['enabled']), None)
                          or (pipelines[0]['source_handle'] if pipelines else None)),
        "account_username": (next((p['account_username'] for p in pipelines if p['enabled']), None)
                             or (pipelines[0]['account_username'] if pipelines else None)),
        "poll_interval_sec": (next((p['poll_interval_sec'] for p in pipelines if p['enabled']), None)
                              or (pipelines[0]['poll_interval_sec'] if pipelines else None)),
        "message": "\n".join(lines) if pipelines else f"No automations configured yet.\n{key_line}"
    }


def tool_auto_setup(
    source_handle: str = None,
    account_username: str = None,
    account_id: str = None,
    poll_interval_sec: int = 300,
    max_posts_per_run: int = 2,
    content_type: str = "feed",
    media_only: bool = True,
    include_reposts: bool = False,
    bluesky_handle: str = None,
    bluesky_app_password: str = None,
    enabled: bool = None,
    name: str = None
) -> dict:
    """
    Configure an autonomous pipeline.
    Use a unique `name` per source (e.g. name='coreiq', name='dailymotivator').
    If name is omitted, uses the source_handle slug or 'default'.
    """
    if source_handle:
        source_handle = source_handle.lstrip('@')
        if '.' not in source_handle:
            source_handle = source_handle + '.bsky.social'
    # auto-name from source so multiple sources don't overwrite each other
    if not name:
        if source_handle:
            name = source_handle.split('.')[0].lower()
        else:
            name = 'default'

    cfg = _load_auto_config(name) or {'name': name}
    cfg['name'] = name
    if source_handle:
        cfg['source_handle'] = source_handle
    if account_username:
        cfg['account_username'] = account_username.lstrip('@').replace('ig_', '')
    # Always resolve to a real Zernio mongo id
    resolved = resolve_instagram_account_id(account_id, cfg.get('account_username') or account_username)
    if resolved:
        cfg['account_id'] = resolved
    elif account_id and _looks_like_zernio_id(account_id):
        cfg['account_id'] = account_id
    if poll_interval_sec:
        cfg['poll_interval_sec'] = int(poll_interval_sec)
    if max_posts_per_run:
        cfg['max_posts_per_run'] = int(max_posts_per_run)
    if content_type:
        cfg['content_type'] = content_type
    cfg['media_only'] = media_only
    cfg['include_reposts'] = include_reposts
    if bluesky_handle:
        cfg['bluesky_handle'] = bluesky_handle
    if bluesky_app_password:
        cfg['bluesky_app_password'] = bluesky_app_password
    if enabled is not None:
        cfg['enabled'] = bool(enabled)
    ok = _save_auto_config(cfg)
    if not ok:
        return {"success": False, "error": "Failed to save config"}
    if cfg.get('enabled'):
        start_auto_pilot()
    return {
        "success": True,
        "config": {k: v for k, v in cfg.items() if k != 'bluesky_app_password'},
        "message": f"Pipeline '{name}' saved · enabled={cfg.get('enabled')} · @{cfg.get('source_handle')} → {cfg.get('account_username') or cfg.get('account_id')}"
    }


def _resolve_pipeline_name(query: str):
    """
    Match pipeline by exact name, source_handle, or partial
    (e.g. picker.foryou.club → name 'picker').
    """
    if not query:
        return None
    q = str(query).strip().lstrip('@').lower()
    configs = _list_auto_configs()
    if not configs:
        return None
    for c in configs:
        if (c.get('name') or '').lower() == q:
            return c.get('name')
    for c in configs:
        src = (c.get('source_handle') or '').lower().lstrip('@')
        if src == q or src == q + '.bsky.social':
            return c.get('name')
    for c in configs:
        name = (c.get('name') or '').lower()
        src = (c.get('source_handle') or '').lower()
        if q in name or q in src or name in q:
            return c.get('name')
        if src and src.split('.')[0] == q.split('.')[0]:
            return c.get('name')
    return None


def tool_auto_start(name: str = None) -> dict:
    """Enable all pipelines (or one by name/handle) and start the engine."""
    configs = _list_auto_configs()
    if name:
        resolved = _resolve_pipeline_name(name)
        if not resolved:
            existing = [c.get('name') for c in configs]
            return {
                "success": False,
                "error": f"Pipeline '{name}' not found",
                "existing": existing,
                "message": f"Pipeline '{name}' not found. Existing: {', '.join(existing) or 'none'}"
            }
        configs = [c for c in configs if c.get('name') == resolved]
    if not configs:
        return {"success": False, "error": "No pipelines configured. Use auto_setup first."}

    results = []
    for cfg in configs:
        cfg['enabled'] = True
        _save_auto_config(cfg)
        once = run_auto_once(cfg.get('name') or 'default')
        results.append({"name": cfg.get('name'), "run": once})
    start_auto_pilot()
    names = ', '.join(c.get('name') or '?' for c in configs)
    return {
        "success": True,
        "message": f"Auto ON for: {names}",
        "runs": results
    }


def tool_auto_stop(name: str = None) -> dict:
    """Disable all pipelines (or one by name/handle). Stops engine if none remain enabled."""
    configs = _list_auto_configs()
    if name:
        resolved = _resolve_pipeline_name(name)
        if not resolved:
            existing = [c.get('name') for c in configs]
            return {
                "success": False,
                "error": f"Pipeline '{name}' not found",
                "existing": existing,
                "message": f"Pipeline '{name}' not found. Existing: {', '.join(existing) or 'none'}"
            }
        configs = [c for c in configs if c.get('name') == resolved]
    if not configs and name:
        return {"success": False, "error": f"Pipeline '{name}' not found"}
    stopped_names = []
    for cfg in configs:
        cfg['enabled'] = False
        _save_auto_config(cfg)
        stopped_names.append(cfg.get('name'))
    still_on = [c for c in _list_auto_configs() if c.get('enabled')]
    if not still_on:
        stop_auto_pilot()
        return {"success": True, "message": f"Stopped {', '.join(stopped_names)}. All automations stopped."}
    return {
        "success": True,
        "message": f"Stopped {', '.join(stopped_names)}. {len(still_on)} still running."
    }


def tool_auto_run_now(name: str = None) -> dict:
    if name:
        resolved = _resolve_pipeline_name(name) or name
        return run_auto_once(resolved)
    configs = [c for c in _list_auto_configs() if c.get('enabled')]
    if not configs:
        configs = _list_auto_configs()
    if not configs:
        return {"success": False, "error": "No pipelines configured"}
    results = []
    for cfg in configs:
        results.append(run_auto_once(cfg.get('name') or 'default'))
    ok = sum(1 for r in results if r.get('success'))
    return {
        "success": ok > 0,
        "message": f"Ran {len(results)} pipeline(s), {ok} ok",
        "results": results
    }


def tool_auto_remove(name: str) -> dict:
    """
    Permanently DELETE a pipeline by name/handle (config + seen history).
    Use for 'remove pipeline X' / 'delete pipeline X' — not for 'stop'.
    """
    if not name or not str(name).strip():
        return {"success": False, "error": "Provide the pipeline name to remove"}
    match = _resolve_pipeline_name(name)
    if not match:
        existing = [c.get('name') for c in _list_auto_configs()]
        return {
            "success": False,
            "error": f"Pipeline '{name}' not found",
            "existing": existing,
            "message": f"No pipeline matching '{name}'. Existing: {', '.join(existing) or 'none'}"
        }
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute("DELETE FROM auto_seen WHERE config_name = %s", (match,))
        cur.execute("DELETE FROM auto_config WHERE name = %s", (match,))
        conn.commit()
        cur.close()
        conn.close()

        still_on = [c for c in _list_auto_configs() if c.get('enabled')]
        if not still_on:
            stop_auto_pilot()

        print(f"🗑️ Removed pipeline '{match}'")
        return {
            "success": True,
            "removed": match,
            "message": f"Pipeline '{match}' permanently removed"
        }
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}



















# ============================================================
# PERMANENT ACCOUNT DELETION TOOLS
# ============================================================

# ============================================================
# PERMANENT ACCOUNT DELETION TOOLS (FIXED)
# ============================================================

def tool_delete_account_permanently(account_identifier: str = None, account_id: str = None, platform: str = 'instagram') -> dict:
    """
    PERMANENTLY DELETE an account from the database.
    This removes all data: the account row, posted_posts, and vault references.
    Use with EXTREME caution — this cannot be undone.
    Use when user says 'delete permanently', 'remove forever', 'erase account', etc.
    """
    if not account_identifier and not account_id:
        return {
            "success": False,
            "error": "No account identifier provided",
            "message": "Please specify which account to permanently delete (username or account_id)."
        }
    
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        
        cur = conn.cursor()
        
        # First, find the account
        if account_id:
            cur.execute("""
                SELECT id, account_id, platform, display_name, username, api_key, is_active
                FROM zernio_accounts 
                WHERE account_id = %s AND platform = %s
            """, (account_id, platform))
        else:
            identifier = account_identifier.lstrip('@').strip()
            cur.execute("""
                SELECT id, account_id, platform, display_name, username, api_key, is_active
                FROM zernio_accounts 
                WHERE (LOWER(username) = LOWER(%s) 
                       OR LOWER(display_name) = LOWER(%s)
                       OR LOWER(username) LIKE LOWER(%s)
                       OR LOWER(display_name) LIKE LOWER(%s))
                  AND platform = %s
            """, (identifier, identifier, f"%{identifier}%", f"%{identifier}%", platform))
        
        row = cur.fetchone()
        
        if not row:
            cur.close()
            conn.close()
            return {
                "success": False,
                "error": "Account not found",
                "message": f"Could not find {platform} account matching '{account_identifier or account_id}'"
            }
        
        db_id, acc_id, plat, display_name, username, api_key, is_active = row
        
        # Get account info for the response
        account_info = f"@{username or display_name or acc_id} ({plat})"
        
        # Count related records before deletion
        if username:
            cur.execute("SELECT COUNT(*) FROM vault WHERE author = %s", (username,))
            vault_count = cur.fetchone()[0]
        else:
            vault_count = 0
        
        # --- PERMANENT DELETE (with proper order) ---
        
        # 1. FIRST: Delete posted_posts that reference vault entries for this account
        if username:
            # Get vault IDs for this account
            cur.execute("SELECT id FROM vault WHERE author = %s", (username,))
            vault_ids = [r[0] for r in cur.fetchall()]
            
            if vault_ids:
                # Delete posted_posts that reference these vault IDs
                placeholders = ','.join(['%s'] * len(vault_ids))
                cur.execute(f"DELETE FROM posted_posts WHERE vault_id IN ({placeholders})", vault_ids)
                posted_deleted = cur.rowcount
            else:
                posted_deleted = 0
            
            # 2. Delete vault entries for this account
            cur.execute("DELETE FROM vault WHERE author = %s", (username,))
            vault_deleted = cur.rowcount
        else:
            vault_deleted = 0
            posted_deleted = 0
        
        # 3. Remove auto_config entries for this account
        if username:
            cur.execute("DELETE FROM auto_config WHERE account_username = %s", (username,))
        
        # 4. Remove auto_seen entries (cleanup)
        if username:
            cur.execute("DELETE FROM auto_seen WHERE config_name IN (SELECT name FROM auto_config WHERE account_username = %s)", (username,))
        
        # 5. FINALLY: Delete the account itself
        cur.execute("DELETE FROM zernio_accounts WHERE id = %s", (db_id,))
        account_deleted = cur.rowcount
        
        conn.commit()
        cur.close()
        conn.close()
        
        return {
            "success": True,
            "permanently_deleted": True,
            "account": account_info,
            "account_id": acc_id,
            "username": username,
            "platform": plat,
            "vault_posts_deleted": vault_deleted,
            "posted_posts_deleted": posted_deleted,
            "message": f"🔥 PERMANENTLY DELETED account {account_info}.\n"
                       f"   • Vault posts removed: {vault_deleted}\n"
                       f"   • Posted history removed: {posted_deleted}\n"
                       f"   ⚠️ This cannot be undone. To re-add, refresh accounts from Zernio."
        }
        
    except Exception as e:
        traceback.print_exc()
        conn.rollback() if conn else None
        return {"success": False, "error": str(e)}


def tool_delete_all_accounts_permanently(platform: str = None, confirm: str = None) -> dict:
    """
    PERMANENTLY DELETE ALL accounts from the database.
    Requires confirmation to prevent accidents.
    """
    # Safety check - require explicit confirmation
    if confirm != "YES_DELETE_ALL":
        return {
            "success": False,
            "error": "Confirmation required",
            "message": "⚠️ This will permanently delete ALL accounts. Reply with 'YES_DELETE_ALL' to confirm."
        }
    
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        
        cur = conn.cursor()
        
        # Get accounts to delete
        if platform:
            cur.execute("""
                SELECT id, account_id, platform, display_name, username 
                FROM zernio_accounts 
                WHERE platform = %s
            """, (platform,))
        else:
            cur.execute("""
                SELECT id, account_id, platform, display_name, username 
                FROM zernio_accounts
            """)
        
        accounts = cur.fetchall()
        
        if not accounts:
            return {
                "success": True,
                "deleted_count": 0,
                "message": "No accounts found to delete."
            }
        
        account_names = []
        usernames = []
        for acc in accounts:
            account_names.append(f"@{acc[4] or acc[3] or acc[1]} ({acc[2]})")
            if acc[4]:
                usernames.append(acc[4])
        
        # 1. FIRST: Delete posted_posts that reference vault entries for these accounts
        if usernames:
            # Get all vault IDs for these accounts
            placeholders = ','.join(['%s'] * len(usernames))
            cur.execute(f"SELECT id FROM vault WHERE author IN ({placeholders})", usernames)
            vault_ids = [r[0] for r in cur.fetchall()]
            
            if vault_ids:
                # Delete posted_posts referencing these vault IDs
                id_placeholders = ','.join(['%s'] * len(vault_ids))
                cur.execute(f"DELETE FROM posted_posts WHERE vault_id IN ({id_placeholders})", vault_ids)
                posted_deleted = cur.rowcount
            else:
                posted_deleted = 0
            
            # 2. Delete vault posts for these accounts
            cur.execute(f"DELETE FROM vault WHERE author IN ({placeholders})", usernames)
            vault_deleted = cur.rowcount
        else:
            vault_deleted = 0
            posted_deleted = 0
        
        # 3. Delete auto_config for these accounts
        if usernames:
            placeholders = ','.join(['%s'] * len(usernames))
            cur.execute(f"DELETE FROM auto_config WHERE account_username IN ({placeholders})", usernames)
        
        # 4. Delete auto_seen entries (cleanup)
        if usernames:
            placeholders = ','.join(['%s'] * len(usernames))
            cur.execute(f"DELETE FROM auto_seen WHERE config_name IN (SELECT name FROM auto_config WHERE account_username IN ({placeholders}))", usernames)
        
        # 5. FINALLY: Delete all accounts
        if platform:
            cur.execute("DELETE FROM zernio_accounts WHERE platform = %s", (platform,))
        else:
            cur.execute("DELETE FROM zernio_accounts")
        
        deleted_count = cur.rowcount
        
        conn.commit()
        cur.close()
        conn.close()
        
        return {
            "success": True,
            "permanently_deleted": True,
            "deleted_count": deleted_count,
            "deleted_accounts": account_names,
            "vault_posts_deleted": vault_deleted,
            "posted_posts_deleted": posted_deleted,
            "message": f"🔥 PERMANENTLY DELETED {deleted_count} account(s): {', '.join(account_names)}\n"
                       f"   • Vault posts removed: {vault_deleted}\n"
                       f"   • Posted history removed: {posted_deleted}\n"
                       f"   ⚠️ This cannot be undone!"
        }
        
    except Exception as e:
        traceback.print_exc()
        conn.rollback() if conn else None
        return {"success": False, "error": str(e)}













# ============================================================
# IMAGE HELPERS
# ============================================================

def convert_image_to_jpeg(image_url):
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            return None
        img = Image.open(BytesIO(response.content))
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        output = BytesIO()
        img.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        return output
    except Exception as e:
        print(f"Image convert error: {e}")
        return None


def fix_image_for_feed(image_bytes, target_ratio='4:5'):
    try:
        img = Image.open(image_bytes)
        width, height = img.size
        target = 0.8
        current = width / height
        if current > target:
            new_height = min(height, 1350)
            new_width = int(new_height * target)
        else:
            new_width = min(width, 1080)
            new_height = int(new_width / target)
        img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', (new_width, new_height), (0, 0, 0))
        x = (new_width - img.width) // 2
        y = (new_height - img.height) // 2
        canvas.paste(img, (x, y))
        output = BytesIO()
        canvas.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        return output
    except Exception:
        return image_bytes


def fix_image_for_story(image_bytes):
    try:
        img = Image.open(image_bytes)
        width, height = img.size
        target = 9 / 16
        current = width / height
        story_width, story_height = 1080, 1920
        if current > target:
            new_height = story_height
            new_width = int(new_height * current)
        else:
            new_width = story_width
            new_height = int(new_width / current)
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', (story_width, story_height), (0, 0, 0))
        x = (story_width - img.width) // 2
        y = (story_height - img.height) // 2
        canvas.paste(img, (x, y))
        output = BytesIO()
        canvas.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        return output
    except Exception:
        return image_bytes

# ============================================================
# ZERNIO HELPERS
# ============================================================

def get_zernio_headers():
    if not ZERNIO_API_KEY:
        return {}
    return {
        "Authorization": f"Bearer {ZERNIO_API_KEY}",
        "Content-Type": "application/json"
    }
















def get_zernio_accounts():
    """Get all Zernio accounts from all API keys."""
    return refresh_all_zernio_accounts()




















def get_account_id_for_platform(platform, account_id=None):
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT account_id FROM zernio_accounts WHERE account_id = %s AND platform = %s AND is_active = TRUE LIMIT 1",
                    (account_id, platform)
                )
            else:
                cur.execute(
                    "SELECT account_id FROM zernio_accounts WHERE platform = %s AND is_active = TRUE ORDER BY username NULLS LAST LIMIT 1",
                    (platform,)
                )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return row[0]
    except Exception:
        pass
    accounts = get_zernio_accounts()
    for acc in accounts:
        if acc.get('platform') == platform:
            if account_id and acc.get('_id') != account_id:
                continue
            return acc.get('_id')
    return None


def _looks_like_zernio_id(value):
    """Real Zernio ids are 24-char hex mongo ids, not usernames."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if v.startswith('ig_') or '@' in v or ' ' in v:
        return False
    if len(v) >= 20 and all(c in '0123456789abcdefABCDEF' for c in v):
        return True
    return False


def resolve_instagram_account_id(account_id=None, account_username=None):
    """
    Always return a real Zernio accountId.
    Rejects fake ids like 'ig_easternfrontdaily'.
    """
    get_zernio_accounts()  # refresh DB

    # If account_id is actually a username, treat it as username
    if account_id and not _looks_like_zernio_id(account_id):
        if not account_username:
            account_username = account_id.replace('ig_', '').lstrip('@')
        account_id = None

    if account_id and _looks_like_zernio_id(account_id):
        # verify it exists
        found = get_account_id_for_platform('instagram', account_id)
        if found:
            return found

    if account_username:
        uname = account_username.replace('ig_', '').lstrip('@').strip()
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT account_id FROM zernio_accounts
                    WHERE platform='instagram' AND is_active=TRUE
                      AND (
                        LOWER(username)=LOWER(%s)
                        OR LOWER(display_name)=LOWER(%s)
                        OR LOWER(display_name) LIKE LOWER(%s)
                        OR LOWER(username) LIKE LOWER(%s)
                      )
                    ORDER BY username NULLS LAST LIMIT 1
                    """,
                    (uname, uname, f"%{uname}%", f"%{uname}%")
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    return row[0]
        except Exception as e:
            print(f"resolve_instagram_account_id: {e}")

        # fallback scan live accounts
        for acc in get_zernio_accounts():
            if acc.get('platform') != 'instagram':
                continue
            u = (acc.get('username') or '').lower()
            d = (acc.get('displayName') or '').lower()
            if uname.lower() in u or uname.lower() in d or u == uname.lower():
                return acc.get('_id')

    return get_account_id_for_platform('instagram')


def get_or_create_zernio_profile(profile_name="Bluesky AI Vault"):
    """Optional helper — posting works without a profile id."""
    try:
        headers = get_zernio_headers()
        if not headers:
            return None
        response = requests.get(f"{ZERNIO_BASE_URL}/profiles", headers=headers, timeout=10)
        print(f"Zernio profiles GET: {response.status_code}")
        if response.status_code == 200:
            profiles = response.json().get('profiles') or response.json().get('data') or []
            if isinstance(profiles, dict):
                profiles = profiles.get('profiles', [])
            for profile in profiles:
                if profile.get('name') == profile_name:
                    return profile.get('_id') or profile.get('id')
            if profiles:
                # use first existing profile
                return profiles[0].get('_id') or profiles[0].get('id')
        payload = {"name": profile_name, "description": "Posts from Bluesky AI Vault"}
        response = requests.post(f"{ZERNIO_BASE_URL}/profiles", headers=headers, json=payload, timeout=10)
        print(f"Zernio profiles POST: {response.status_code} {response.text[:200]}")
        if response.status_code in (200, 201):
            data = response.json()
            return (data.get('profile') or data).get('_id') or (data.get('profile') or data).get('id')
    except Exception as e:
        print(f"Zernio profile error: {e}")
    return None


def upload_media_to_zernio(image_url, content_type='feed'):
    try:
        print(f"📥 Downloading image: {image_url[:80]}...")
        image_bytes = convert_image_to_jpeg(image_url)
        if not image_bytes:
            print("❌ convert_image_to_jpeg failed")
            return None, "Failed to download/convert image"
        fixed = fix_image_for_story(image_bytes) if content_type == 'story' else fix_image_for_feed(image_bytes)
        presign = requests.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=get_zernio_headers(),
            json={"filename": "post.jpg", "contentType": "image/jpeg"},
            timeout=30
        )
        print(f"Zernio presign: {presign.status_code}")
        if presign.status_code not in (200, 201):
            return None, f"Presign failed: {presign.status_code} {presign.text[:200]}"
        data = presign.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')
        if not upload_url or not public_url:
            return None, f"Missing uploadUrl/publicUrl: {data}"
        fixed.seek(0)
        up = requests.put(upload_url, headers={'Content-Type': 'image/jpeg'}, data=fixed, verify=False, timeout=60)
        print(f"Zernio upload PUT: {up.status_code}")
        if up.status_code not in (200, 201, 204):
            return None, f"Upload PUT failed: {up.status_code}"
        return public_url, None
    except Exception as e:
        print(f"Upload media error: {e}")
        traceback.print_exc()
        return None, str(e)


def post_to_zernio(image_url, caption, platforms, scheduled_time=None, content_type='feed', account_ids=None):
    if not platforms and not account_ids:
        return {"success": False, "error": "No platforms selected"}

    # Normalize account_ids → list of instagram account ids
    resolved_ids = []
    if isinstance(account_ids, list):
        resolved_ids = [a for a in account_ids if a]
    elif isinstance(account_ids, dict):
        for v in account_ids.values():
            if isinstance(v, list):
                resolved_ids.extend(v)
            elif v:
                resolved_ids.append(v)

    # If still empty, pick first instagram account
    if not resolved_ids:
        aid = get_account_id_for_platform('instagram')
        if aid:
            resolved_ids = [aid]

    if not resolved_ids:
        # try refresh from Zernio
        get_zernio_accounts()
        aid = get_account_id_for_platform('instagram')
        if aid:
            resolved_ids = [aid]

    if not resolved_ids:
        return {"success": False, "error": "No Instagram account connected in Zernio"}

    try:
        # Profile is optional — do not block posting
        profile_id = get_or_create_zernio_profile()
        if profile_id:
            print(f"Zernio profile: {profile_id}")
        else:
            print("⚠️ No Zernio profile (continuing without it)")

        public_url, upload_err = upload_media_to_zernio(image_url, content_type)
        if not public_url:
            return {"success": False, "error": upload_err or "Failed to upload image"}

        platform_entries = []
        for account_id in resolved_ids:
            entry = {
                "platform": "instagram",
                "accountId": account_id,
                "platformSpecificData": {"contentType": content_type}
            }
            platform_entries.append(entry)

        payload = {
            "mediaItems": [{"type": "image", "url": public_url}],
            "platforms": platform_entries,
            "content": caption or ""
        }
        if profile_id:
            payload["profileId"] = profile_id
        if scheduled_time:
            tz = pytz.timezone(SCHEDULE_TIMEZONE)
            if scheduled_time.tzinfo is None:
                scheduled_time = tz.localize(scheduled_time)
            payload["scheduledFor"] = scheduled_time.astimezone(pytz.UTC).isoformat()
            payload["timezone"] = SCHEDULE_TIMEZONE
        else:
            payload["publishNow"] = True

        print(f"📤 Zernio post payload accounts={[e['accountId'] for e in platform_entries]}")
        response = requests.post(
            f"{ZERNIO_BASE_URL}/posts",
            headers=get_zernio_headers(),
            json=payload,
            timeout=60
        )
        print(f"Zernio posts response: {response.status_code} {response.text[:300]}")
        if response.status_code in (200, 201):
            data = response.json()
            return {
                "success": True,
                "post_id": data.get('post', {}).get('_id') or data.get('_id'),
                "message": "Scheduled" if scheduled_time else "Published"
            }
        error_detail = response.text
        try:
            error_detail = response.json().get('error', error_detail)
        except Exception:
            pass
        return {"success": False, "error": f"Zernio error: {error_detail}"}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}



































# ============================================================
# MULTI-PLATFORM POSTING
# ============================================================

def post_to_bluesky_direct(image_url, caption):
    """Post directly to Bluesky using AT Protocol client."""
    try:
        # Get active Bluesky session
        client = None
        for sid, sess in sessions.items():
            if sess.get('client'):
                client = sess['client']
                break
        
        if not client:
            # Try to restore from DB
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT session_id, session_string, handle 
                    FROM sessions 
                    WHERE expires_at > CURRENT_TIMESTAMP 
                    ORDER BY last_used_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    client = Client()
                    client.login(session_string=row[1])
                    sessions[row[0]] = {
                        'client': client,
                        'handle': row[2],
                        'session_string': row[1]
                    }
        
        if not client:
            return {"success": False, "error": "No active Bluesky session. Please login first."}
        
        # Download and upload image
        image_bytes = convert_image_to_jpeg(image_url)
        if not image_bytes:
            return {"success": False, "error": "Failed to download/process image"}
        
        # Upload blob to Bluesky
        blob = client.upload_blob(image_bytes.read())
        
        # Create post with image
        embed = {
            "$type": "app.bsky.embed.images",
            "images": [{
                "alt": caption[:120] or "Image post",
                "image": blob.blob
            }]
        }
        
        response = client.send_post(text=caption, embed=embed)
        
        return {
            "success": True,
            "post_uri": response.uri,
            "cid": response.cid,
            "platform": "bluesky",
            "message": "✅ Posted to Bluesky successfully!"
        }
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}




























def post_to_zernio_multi_platform(image_url, caption, platforms, scheduled_time=None, content_type='feed', account_username=None):
    """
    Post to Zernio platforms using the correct API key for the account.
    """
    if not platforms:
        return {"success": False, "error": "No platforms specified"}
    
    zernio_platforms = ['instagram']
    platforms = [p for p in platforms if p in zernio_platforms]
    
    if not platforms:
        return {"success": False, "error": "No Zernio platforms specified"}
    
    try:
        # Get account info from DB
        account_info = None
        if account_username:
            try:
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT account_id, api_key, api_key_index FROM zernio_accounts 
                        WHERE username = %s AND platform = %s AND is_active = TRUE
                        LIMIT 1
                    """, (account_username, platforms[0]))
                    row = cur.fetchone()
                    cur.close()
                    conn.close()
                    if row:
                        account_info = {
                            "account_id": row[0], 
                            "api_key": row[1],
                            "api_key_index": row[2]
                        }
            except Exception as e:
                print(f"DB lookup error: {e}")
        
        # If not found, try to resolve from Zernio
        if not account_info:
            result = resolve_zernio_account(account_username, platforms[0])
            if result:
                account_info = result
        
        if not account_info:
            return {"success": False, "error": f"Could not resolve account: {account_username}"}
        
        account_id = account_info['account_id']
        api_key = account_info['api_key']
        api_key_index = account_info.get('api_key_index', '?')
        
        # Use the specific API key for this account
        headers = get_zernio_headers_for_key(api_key)
        if not headers:
            return {"success": False, "error": "Invalid API key for this account"}
        
        print(f"📤 Using Zernio API Key {api_key_index} for account @{account_username}")
        
        # Upload media with correct API key
        image_bytes = convert_image_to_jpeg(image_url)
        if not image_bytes:
            return {"success": False, "error": "Failed to download image"}
        
        fixed = fix_image_for_feed(image_bytes) if content_type == 'feed' else fix_image_for_story(image_bytes)
        
        # Use the specific API key for presign
        presign = requests.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=headers,
            json={"filename": "post.jpg", "contentType": "image/jpeg"},
            timeout=30
        )
        if presign.status_code not in (200, 201):
            return {"success": False, "error": f"Presign failed: {presign.status_code}"}
        
        data = presign.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')
        if not upload_url or not public_url:
            return {"success": False, "error": "Missing upload URL"}
        
        fixed.seek(0)
        up = requests.put(upload_url, headers={'Content-Type': 'image/jpeg'}, data=fixed, verify=False, timeout=60)
        if up.status_code not in (200, 201, 204):
            return {"success": False, "error": f"Upload failed: {up.status_code}"}
        
        # Create platform entries
        platform_entries = []
        for platform in platforms:
            entry = {
                "platform": platform,
                "accountId": account_id,
                "platformSpecificData": {"contentType": content_type}
            }
            platform_entries.append(entry)
        
        # Build payload
        payload = {
            "mediaItems": [{"type": "image", "url": public_url}],
            "platforms": platform_entries,
            "content": caption or ""
        }
        
        if scheduled_time:
            tz = pytz.timezone(SCHEDULE_TIMEZONE)
            if scheduled_time.tzinfo is None:
                scheduled_time = tz.localize(scheduled_time)
            payload["scheduledFor"] = scheduled_time.astimezone(pytz.UTC).isoformat()
            payload["timezone"] = SCHEDULE_TIMEZONE
        else:
            payload["publishNow"] = True
        
        # Post with correct API key
        response = requests.post(
            f"{ZERNIO_BASE_URL}/posts",
            headers=headers,
            json=payload,
            timeout=60
        )
        
        if response.status_code in (200, 201):
            data = response.json()
            return {
                "success": True,
                "post_id": data.get('post', {}).get('_id') or data.get('_id'),
                "platforms": platforms,
                "account_username": account_username,
                "api_key_index": api_key_index,
                "message": f"✅ Posted to {', '.join(platforms)} using API Key {api_key_index}"
            }
        
        return {"success": False, "error": f"Zernio error: {response.text[:200]}"}
        
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    

def resolve_zernio_account(account_username, platform='instagram'):
    """Resolve a Zernio account by username and platform using all API keys."""
    if not account_username:
        return None
    
    # Check DB first
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT account_id, api_key FROM zernio_accounts 
                WHERE username = %s AND platform = %s AND is_active = TRUE
                LIMIT 1
            """, (account_username, platform))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return {"account_id": row[0], "api_key": row[1]}
    except Exception as e:
        print(f"DB error: {e}")
    
    # Try all Zernio API keys
    keys = get_zernio_api_keys()
    for key_info in keys:
        api_key = key_info.get('key')
        headers = get_zernio_headers_for_key(api_key)
        
        if not headers:
            continue
            
        try:
            response = requests.get(
                f"{ZERNIO_BASE_URL}/accounts",
                headers=headers,
                timeout=15
            )
            if response.status_code == 200:
                accounts = response.json().get('accounts', [])
                for acc in accounts:
                    if acc.get('username') == account_username and acc.get('platform') == platform:
                        # Store in DB with API key
                        try:
                            conn = get_db_connection()
                            if conn:
                                cur = conn.cursor()
                                cur.execute("""
                                    INSERT INTO zernio_accounts 
                                    (account_id, platform, display_name, username, profile_picture, api_key, api_key_index, is_active, last_sync)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                                    ON CONFLICT (account_id, platform) DO UPDATE SET
                                        display_name = EXCLUDED.display_name,
                                        username = EXCLUDED.username,
                                        api_key = EXCLUDED.api_key,
                                        api_key_index = EXCLUDED.api_key_index,
                                        is_active = TRUE,
                                        last_sync = CURRENT_TIMESTAMP
                                """, (
                                    acc.get('_id'),
                                    platform,
                                    acc.get('displayName'),
                                    acc.get('username'),
                                    acc.get('profilePicture'),
                                    api_key,
                                    key_info.get('index')
                                ))
                                conn.commit()
                                cur.close()
                                conn.close()
                        except Exception as e:
                            print(f"Save error: {e}")
                        return {"account_id": acc.get('_id'), "api_key": api_key}
        except Exception as e:
            print(f"Zernio API error for key {key_info.get('index')}: {e}")
    
    return None














def mark_post_as_posted(vault_id, uri, platform, platform_post_id=None, status='completed'):
    try:
        conn = get_db_connection()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute("SELECT id FROM posted_posts WHERE uri = %s AND platform = %s", (uri, platform))
        if cur.fetchone():
            cur.execute(
                "UPDATE posted_posts SET vault_id=%s, platform_post_id=%s, status=%s, posted_at=CURRENT_TIMESTAMP WHERE uri=%s AND platform=%s",
                (vault_id, platform_post_id, status, uri, platform)
            )
        else:
            cur.execute(
                "INSERT INTO posted_posts (vault_id, uri, platform, platform_post_id, status, posted_at) VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)",
                (vault_id, uri, platform, platform_post_id, status)
            )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"mark_post error: {e}")
        return False


def is_post_already_posted(uri, platform):
    try:
        conn = get_db_connection()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM posted_posts WHERE uri = %s AND platform = %s AND status = 'completed'",
            (uri, platform)
        )
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except Exception:
        return False

# ============================================================
# BLUESKY HELPERS
# ============================================================

def extract_images_from_embed(embed):
    images = []
    if not embed:
        return images
    if hasattr(embed, 'images') and embed.images:
        for img in embed.images:
            data = {}
            if hasattr(img, 'fullsize'):
                data['url'] = img.fullsize
            elif hasattr(img, 'thumb'):
                data['url'] = img.thumb
            else:
                continue
            data['thumb'] = getattr(img, 'thumb', data['url'])
            data['alt'] = getattr(img, 'alt', '') or ''
            images.append(data)
    return images


def extract_video_from_embed(embed):
    if not embed:
        return None
    if hasattr(embed, 'playlist'):
        return {
            'playlist': embed.playlist,
            'cid': getattr(embed, 'cid', None),
            'thumbnail': getattr(embed, 'thumbnail', None),
            'type': 'hls'
        }
    return None


def is_reply(post):
    try:
        return bool(hasattr(post.record, 'reply') and post.record.reply)
    except Exception:
        return False


def is_repost(post):
    try:
        t = getattr(post.record, '$type', '') or ''
        return 'repost' in t.lower()
    except Exception:
        return False








# ============================================================
# TOOL FUNCTIONS (called by the AI)
# ============================================================

def tool_login(username: str, password: str) -> dict:
    """Login to Bluesky and create a persistent session."""
    try:
        client = Client()
        client.login(username, password)
        profile = client.get_profile(username)
        session_id = f"{username}_{int(datetime.now().timestamp())}"
        session_string = client.export_session_string()
        expires_at = datetime.now() + timedelta(days=30)

        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM sessions WHERE handle = %s', (profile.handle,))
            cur.execute('''
                INSERT INTO sessions (session_id, username, handle, display_name, avatar, session_string, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (session_id, username, profile.handle, profile.display_name or username, profile.avatar, session_string, expires_at))
            conn.commit()
            cur.close()
            conn.close()

        sessions[session_id] = {
            'client': client,
            'username': username,
            'handle': profile.handle,
            'display_name': profile.display_name or username,
            'avatar': profile.avatar,
            'session_string': session_string
        }
        return {
            "success": True,
            "session_id": session_id,
            "handle": profile.handle,
            "display_name": profile.display_name or username,
            "message": f"Logged in as @{profile.handle}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_restore_session(handle: str) -> dict:
    """Restore a previous Bluesky session by handle."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute('''
            SELECT session_id, username, handle, display_name, avatar, session_string, expires_at
            FROM sessions WHERE handle = %s AND expires_at > CURRENT_TIMESTAMP
            ORDER BY last_used_at DESC LIMIT 1
        ''', (handle,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return {"success": False, "error": "No valid session found. Please login again."}

        session_id, username, handle, display_name, avatar, session_string, expires_at = row
        client = Client()
        client.login(session_string=session_string)
        sessions[session_id] = {
            'client': client,
            'username': username,
            'handle': handle,
            'display_name': display_name or handle,
            'avatar': avatar,
            'session_string': session_string
        }
        return {
            "success": True,
            "session_id": session_id,
            "handle": handle,
            "display_name": display_name or handle,
            "message": f"Session restored for @{handle}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_fetch_posts(session_id: str, actor: str, limit: int = 20, include_reposts: bool = False, media_only: bool = True) -> dict:
    """Fetch recent posts from a Bluesky handle."""
    if not session_id or session_id not in sessions:
        return {"success": False, "error": "Not logged in. Please login first."}
    try:
        client = sessions[session_id]['client']
        result = client.get_author_feed(actor=actor, limit=min(limit * 2, 100))
        posts = []
        for item in result.feed:
            post = item.post
            if is_reply(post):
                continue
            if is_repost(post) and not include_reposts:
                continue
            images = extract_images_from_embed(getattr(post, 'embed', None))
            video = extract_video_from_embed(getattr(post, 'embed', None))
            if media_only and not images and not video:
                continue
            posts.append({
                "uri": post.uri,
                "author": post.author.handle,
                "display_name": post.author.display_name or post.author.handle,
                "text": getattr(post.record, 'text', '') or '',
                "likes": getattr(post, 'like_count', 0) or 0,
                "reposts": getattr(post, 'repost_count', 0) or 0,
                "replies": getattr(post, 'reply_count', 0) or 0,
                "created_at": getattr(post.record, 'created_at', ''),
                "images": images,
                "video": video,
                "has_media": bool(images or video)
            })
            if len(posts) >= limit:
                break

        # Cache for a later "save them to vault" without re-passing full posts
        if session_id in sessions:
            sessions[session_id]['_last_fetched'] = posts
            sessions[session_id]['_last_actor'] = actor

        return {
            "success": True,
            "count": len(posts),
            "posts": posts,
            "message": f"Fetched {len(posts)} posts from @{actor}. Say 'save them to the vault' to store them."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_add_to_vault(posts: list = None, handler_handle: str = None, session_id: str = None) -> dict:
    """
    Save posts into the vault.
    Prefer calling with session_id after a fetch — it uses the cached last fetch.
    You can also pass an explicit posts list.
    """
    if (not posts) and session_id and session_id in sessions:
        posts = sessions[session_id].get('_last_fetched') or []
        handler_handle = handler_handle or sessions[session_id].get('_last_actor')

    if not posts:
        return {
            "success": False,
            "error": "No posts to save. Fetch posts first, then say 'save them to the vault'."
        }
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        saved = 0
        for post in posts:
            images_json = Json(post.get('images', []))
            video_json = Json(post.get('video')) if post.get('video') else None
            cur.execute('''
                INSERT INTO vault (uri, author, display_name, text, images, video, likes, reposts, replies, created_at, handler_handle)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (uri) DO UPDATE SET
                    text = EXCLUDED.text,
                    images = EXCLUDED.images,
                    video = EXCLUDED.video,
                    likes = EXCLUDED.likes,
                    reposts = EXCLUDED.reposts,
                    replies = EXCLUDED.replies
            ''', (
                post.get('uri'),
                post.get('author'),
                post.get('display_name'),
                post.get('text'),
                images_json,
                video_json,
                post.get('likes', 0),
                post.get('reposts', 0),
                post.get('replies', 0),
                post.get('created_at'),
                handler_handle or post.get('author')
            ))
            saved += 1
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "saved": saved, "message": f"Saved {saved} post(s) to vault"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_list_vault(limit: int = 30, handler_handle: str = None) -> dict:
    """List posts currently in the vault."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        if handler_handle:
            cur.execute(
                "SELECT id, uri, author, display_name, text, images, likes, saved_at, notes FROM vault WHERE handler_handle = %s ORDER BY saved_at DESC LIMIT %s",
                (handler_handle, limit)
            )
        else:
            cur.execute(
                "SELECT id, uri, author, display_name, text, images, likes, saved_at, notes FROM vault ORDER BY saved_at DESC LIMIT %s",
                (limit,)
            )
        rows = cur.fetchall()
        items = []
        for r in rows:
            imgs = r[5]
            n_img = 0
            if imgs:
                try:
                    n_img = len(imgs) if isinstance(imgs, list) else 1
                except Exception:
                    n_img = 0
            items.append({
                "id": r[0],
                "uri": r[1],
                "author": r[2],
                "display_name": r[3],
                "text": (r[4] or '')[:200],
                "images": imgs,
                "image_count": n_img,
                "likes": r[6],
                "saved_at": str(r[7]) if r[7] else None,
                "notes": r[8]
            })
        cur.close()
        conn.close()

        if not items:
            msg = "Your vault is empty right now — nothing to post yet."
        else:
            lines = [f"Here are the latest {len(items)} post(s) in your vault:"]
            for i, it in enumerate(items, 1):
                preview = (it.get('text') or '').strip().replace('\n', ' ')
                if len(preview) > 90:
                    preview = preview[:90] + "…"
                if not preview:
                    preview = "(image only)"
                media = f" · {it.get('image_count') or 0} image(s)" if it.get('image_count') else ""
                lines.append(
                    f"{i}. id {it['id']} — @{it.get('author') or '?'} — \"{preview}\"{media}"
                )
            lines.append("Say e.g. “post the 1st” or “post id 12” and I’ll publish it to Instagram.")
            msg = "\n".join(lines)

        return {"success": True, "count": len(items), "vault": items, "message": msg}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_list_vault_by_status(status=None, limit=50, offset=0):
    """List vault items filtered by post status."""
    conn = get_db_connection()
    if not conn:
        return {"success": False, "error": "DB unavailable"}
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if status == 'unposted':
            cur.execute("""
                SELECT v.id, v.uri, v.author, v.display_name, v.text, v.images, v.video, 
                       v.likes, v.reposts, v.replies, v.created_at, v.saved_at, 
                       v.handler_handle, v.notes,
                       NULL as post_status, NULL as posted_at, NULL as platform_post_id
                FROM vault v
                WHERE NOT EXISTS (
                    SELECT 1 FROM posted_posts p 
                    WHERE p.uri = v.uri AND p.status IN ('completed', 'posted')
                )
                ORDER BY v.saved_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        elif status in ('posted', 'completed'):
            cur.execute("""
                SELECT v.id, v.uri, v.author, v.display_name, v.text, v.images, v.video, 
                       v.likes, v.reposts, v.replies, v.created_at, v.saved_at, 
                       v.handler_handle, v.notes,
                       p.status as post_status, p.posted_at, p.platform_post_id
                FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status IN ('completed', 'posted')
                ORDER BY p.posted_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        elif status == 'scheduled':
            cur.execute("""
                SELECT v.id, v.uri, v.author, v.display_name, v.text, v.images, v.video, 
                       v.likes, v.reposts, v.replies, v.created_at, v.saved_at, 
                       v.handler_handle, v.notes,
                       p.status as post_status, p.posted_at, p.platform_post_id
                FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status = 'scheduled'
                ORDER BY p.posted_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        else:
            cur.execute("""
                SELECT v.id, v.uri, v.author, v.display_name, v.text, v.images, v.video, 
                       v.likes, v.reposts, v.replies, v.created_at, v.saved_at, 
                       v.handler_handle, v.notes,
                       COALESCE(p.status, 'unposted') as post_status, 
                       p.posted_at, p.platform_post_id
                FROM vault v
                LEFT JOIN posted_posts p ON p.uri = v.uri
                ORDER BY v.saved_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        
        rows = cur.fetchall()
        
        # Get total count for the filtered query
        if status == 'unposted':
            cur.execute("""
                SELECT COUNT(*) FROM vault v
                WHERE NOT EXISTS (
                    SELECT 1 FROM posted_posts p 
                    WHERE p.uri = v.uri AND p.status IN ('completed', 'posted')
                )
            """)
        elif status in ('posted', 'completed'):
            cur.execute("""
                SELECT COUNT(*) FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status IN ('completed', 'posted')
            """)
        elif status == 'scheduled':
            cur.execute("""
                SELECT COUNT(*) FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status = 'scheduled'
            """)
        else:
            cur.execute("SELECT COUNT(*) FROM vault")
        
        total = cur.fetchone()['count']
        cur.close()
        conn.close()
        
        vault = []
        for r in rows:
            vault.append({
                "id": r['id'],
                "uri": r['uri'],
                "author": r['author'],
                "display_name": r['display_name'],
                "text": r['text'],
                "images": r['images'] or [],
                "video": r['video'],
                "likes": r['likes'],
                "reposts": r['reposts'],
                "replies": r['replies'],
                "created_at": r['created_at'].isoformat() if r['created_at'] else None,
                "saved_at": r['saved_at'].isoformat() if r['saved_at'] else None,
                "handler_handle": r['handler_handle'],
                "notes": r['notes'],
                "post_status": r.get('post_status') or 'unposted',
                "posted_at": r['posted_at'].isoformat() if r.get('posted_at') else None,
                "platform_post_id": r.get('platform_post_id'),
            })
        return {"success": True, "vault": vault, "count": total, "status_filter": status or 'all'}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_delete_vault_items(ids=None, status=None, all=False):
    """Delete vault items by ID, by status, or all."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        
        cur = conn.cursor()
        deleted_count = 0
        deleted_uris = []
        
        if ids and isinstance(ids, list):
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT id, uri FROM vault WHERE id IN ({placeholders})", ids)
            items = cur.fetchall()
        elif status == 'unposted':
            cur.execute("""
                SELECT id, uri FROM vault v
                WHERE NOT EXISTS (
                    SELECT 1 FROM posted_posts p 
                    WHERE p.uri = v.uri AND p.status IN ('completed', 'posted')
                )
            """)
            items = cur.fetchall()
        elif status in ('posted', 'completed'):
            cur.execute("""
                SELECT v.id, v.uri FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status IN ('completed', 'posted')
            """)
            items = cur.fetchall()
        elif status == 'scheduled':
            cur.execute("""
                SELECT v.id, v.uri FROM vault v
                INNER JOIN posted_posts p ON p.uri = v.uri
                WHERE p.status = 'scheduled'
            """)
            items = cur.fetchall()
        elif all:
            cur.execute("SELECT id, uri FROM vault")
            items = cur.fetchall()
        else:
            return {"success": False, "error": "Specify ids, status, or all=True"}
        
        if not items:
            cur.close()
            conn.close()
            return {"success": True, "deleted_count": 0, "message": "No items to delete"}
        
        for item in items:
            item_id, uri = item
            cur.execute("DELETE FROM posted_posts WHERE uri = %s", (uri,))
            cur.execute("DELETE FROM vault WHERE id = %s", (item_id,))
            deleted_count += 1
            deleted_uris.append(uri)
        
        conn.commit()
        cur.close()
        conn.close()
        
        return {
            "success": True,
            "deleted_count": deleted_count,
            "deleted_uris": deleted_uris,
            "message": f"Deleted {deleted_count} item(s) from vault"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_post_unposted(account_id=None, account_username=None, limit=10):
    """Post all unposted vault items to Instagram."""
    result = tool_list_vault_by_status(status='unposted', limit=limit)
    if not result.get('success'):
        return result
    
    items = result.get('vault', [])
    if not items:
        return {"success": True, "posted_count": 0, "message": "No unposted items to post"}
    
    posted = 0
    errors = []
    results = []
    
    for item in items:
        res = tool_post_now(
            vault_id=item.get('id'),
            account_id=account_id,
            account_username=account_username
        )
        results.append(res)
        if res.get('success'):
            posted += 1
        else:
            errors.append(res.get('error', 'Unknown error'))
        time.sleep(1.5)
    
    return {
        "success": posted > 0,
        "posted_count": posted,
        "total": len(items),
        "results": results,
        "errors": errors,
        "message": f"Posted {posted}/{len(items)} unposted items to Instagram"
    }
def tool_remove_from_vault(uri: str) -> dict:
    """Remove a post from the vault by URI."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute("DELETE FROM vault WHERE uri = %s", (uri,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "deleted": deleted, "message": "Removed from vault" if deleted else "URI not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_get_status() -> dict:
    """Return high-level counts: vault size, scheduled, posted, accounts."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM vault")
        vault_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vault WHERE notes LIKE '%Scheduled for%'")
        scheduled_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM posted_posts WHERE status = 'completed'")
        posted_count = cur.fetchone()[0]
        
        # CHANGED: Only count Instagram/Zernio accounts (not Bluesky sessions)
        cur.execute("SELECT COUNT(*) FROM zernio_accounts WHERE is_active = TRUE AND platform = 'instagram'")
        accounts_count = cur.fetchone()[0]
        
        # Keep Bluesky session info for fetching
        cur.execute("SELECT handle FROM sessions WHERE expires_at > CURRENT_TIMESTAMP ORDER BY last_used_at DESC LIMIT 1")
        row = cur.fetchone()
        active_handle = row[0] if row else None
        cur.close()
        conn.close()
        return {
            "success": True,
            "vault_count": vault_count,
            "scheduled_count": scheduled_count,
            "posted_count": posted_count,
            "accounts_count": accounts_count,  # Now only Instagram accounts
            "active_handle": active_handle,    # Bluesky session for fetching
            "message": f"Vault: {vault_count} · Scheduled: {scheduled_count} · Posted: {posted_count}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
















def tool_list_accounts(platform: str = None) -> dict:
    """
    List ALL connected accounts across every Zernio API key in .env.
    Always refreshes from Zernio first so KEY1 + KEY2 accounts both appear.
    """
    try:
        # Sync every key's accounts into DB (KEY1 + KEY2 + …)
        ensure_zernio_keys_loaded()
        refresh_all_zernio_accounts()

        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}

        cur = conn.cursor()
        if platform:
            cur.execute("""
                SELECT account_id, platform, display_name, username, profile_picture,
                       api_key, api_key_index, is_active
                FROM zernio_accounts
                WHERE platform = %s AND is_active = TRUE
                ORDER BY api_key_index NULLS LAST, username
            """, (platform,))
        else:
            cur.execute("""
                SELECT account_id, platform, display_name, username, profile_picture,
                       api_key, api_key_index, is_active
                FROM zernio_accounts
                WHERE is_active = TRUE
                ORDER BY api_key_index NULLS LAST, platform, username
            """)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        accounts = []
        for row in rows:
            key_preview = ''
            if row[5]:
                key_preview = (row[5][:12] + '…') if len(row[5]) > 12 else row[5]
            accounts.append({
                "account_id": row[0],
                "platform": row[1] or 'instagram',
                "display_name": row[2],
                "username": row[3],
                "profile_picture": row[4],
                "api_key_preview": key_preview,
                "api_key_index": row[6],
                "is_active": row[7],
            })

        if not accounts:
            return {
                "success": True,
                "count": 0,
                "accounts": [],
                "message": (
                    "No connected accounts found.\n\n"
                    "To connect accounts:\n"
                    "• Instagram: connect in Zernio first, ensure ZERNIO_API_KEY1/2 are in .env\n"
                    "• Bluesky: Login with [handle] and [app-password]"
                ),
            }

        # Group by API key index, then platform
        by_key = {}
        for acc in accounts:
            idx = acc.get('api_key_index') or 0
            by_key.setdefault(idx, []).append(acc)

        lines = [f"Connected accounts ({len(accounts)} total, across {len(by_key)} API key(s)):"]
        for idx in sorted(by_key.keys()):
            lines.append(f"\n🔑 ZERNIO_API_KEY{idx or '?'}:")
            for a in by_key[idx]:
                uname = a.get('username') or '?'
                dname = a.get('display_name') or uname
                plat = (a.get('platform') or 'instagram').capitalize()
                aid = a.get('account_id') or ''
                lines.append(f"  • @{uname} ({dname}) — {plat} — id={aid}")

        # Also group by platform for summary
        platforms = sorted({a.get('platform') or 'instagram' for a in accounts})

        return {
            "success": True,
            "count": len(accounts),
            "accounts": accounts,
            "platforms": platforms,
            "keys_used": sorted(by_key.keys()),
            "message": "\n".join(lines),
        }

    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}

















def is_platform_connected(platform: str) -> bool:
    """Check if a specific platform has any connected accounts."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM zernio_accounts 
            WHERE platform = %s AND is_active = TRUE
        """, (platform,))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count > 0
    except Exception:
        return False

def is_bluesky_connected() -> bool:
    """Check if Bluesky session exists."""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM sessions 
                WHERE expires_at > CURRENT_TIMESTAMP
            """)
            count = cur.fetchone()[0]
            cur.close()
            conn.close()
            return count > 0
    except Exception:
        return False








def tool_list_api_keys():
    """List all configured Zernio API keys from .env and their auto-detected accounts."""
    status = ensure_zernio_keys_loaded()
    keys = status.get('keys') or []

    if not keys:
        return {
            "success": True,
            "message": "⚠️ No Zernio API keys configured in .env\n"
                       "Set ZERNIO_API_KEY, ZERNIO_API_KEY1/ZERNIO_API_KEY2, or ZERNIO_API_KEYS=key1,key2",
            "keys": [],
            "count": 0,
            "total_accounts": 0
        }

    lines = ["🔑 Zernio API Keys (from .env):"]
    total_accounts = 0

    for key_info in keys:
        index = key_info.get('index')
        env_var = key_info.get('env_var', f'ZERNIO_API_KEY{index}')
        accounts = key_info.get('accounts', [])
        key_preview = (key_info.get('key') or '')[:16] + '…' if key_info.get('key') else 'None'

        lines.append(f"  • {env_var}: {key_preview}")
        if accounts:
            account_names = [f"@{a.get('username')} ({a.get('platform')})" for a in accounts]
            lines.append(f"    Accounts: {', '.join(account_names)}")
            total_accounts += len(accounts)
        else:
            lines.append("    Accounts: (none detected — check API key validity)")

    lines.append(f"\n  Total: {len(keys)} API key(s) from .env")
    lines.append(f"  Total accounts: {total_accounts}")
    lines.append("  Each key supports up to 2 accounts")

    return {
        "success": True,
        "message": "\n".join(lines),
        "keys": keys,
        "count": len(keys),
        "total_accounts": total_accounts
    }








def tool_post_now(
    vault_id: int = None,
    uri: str = None,
    image_url: str = None,
    caption: str = "",
    content_type: str = "feed",
    platforms: list = None,
    account_id: str = None,
    account_username: str = None
) -> dict:
    """
    Post a vault item (or raw image) to social media.
    Posts to Instagram via Zernio only.
    Prefer vault_id (integer from list_vault). 
    Optional account_username e.g. 'easternfrontdaily' for Zernio platforms.
    """
    if not platforms:
        platforms = ['instagram']
    elif isinstance(platforms, str):
        platforms = [platforms]
    
    # Filter valid platforms
    valid_platforms = ['instagram']
    platforms = [p for p in platforms if p in valid_platforms]
    
    if not platforms:
        return {"success": False, "error": "No valid platforms specified"}
    
    try:
        # Resolve vault row by id or uri
        if (vault_id or uri) and not image_url:
            conn = get_db_connection()
            if not conn:
                return {"success": False, "error": "Database unavailable"}
            cur = conn.cursor()
            if vault_id:
                cur.execute("SELECT id, uri, text, images, author FROM vault WHERE id = %s", (int(vault_id),))
            else:
                cur.execute("SELECT id, uri, text, images, author FROM vault WHERE uri = %s", (uri,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if not row:
                return {"success": False, "error": f"Vault post not found (id={vault_id}, uri={uri})"}
            vault_id, uri, text, images, author = row
            
            # Check if already posted to any of the requested platforms
            already_posted = []
            for p in platforms:
                if is_post_already_posted(uri, p):
                    already_posted.append(p)
            if already_posted:
                return {"success": False, "error": f"Already posted to {', '.join(already_posted)}"}
            
            if images and len(images) > 0:
                image_url = images[0].get('url') if isinstance(images[0], dict) else None
            if not image_url:
                return {"success": False, "error": "No image on this vault post"}
            if not caption:
                caption = f"{(text or '')[:200]}" if text else f"Post from @{author}"

        if not image_url:
            return {"success": False, "error": "Provide vault_id, uri, or image_url"}

        results = []
        
        # Split platforms by strategy
        zernio_platforms = ['instagram']
        bluesky_platform = 'bluesky'
        
        zernio_to_post = [p for p in platforms if p in zernio_platforms]
        bluesky_to_post = [p for p in platforms if p == bluesky_platform]
        
        # 1. Post to Instagram via Zernio
        if zernio_to_post:
            # Auto-pick first Instagram account if username not given
            if not account_username:
                try:
                    conn_a = get_db_connection()
                    if conn_a:
                        cur_a = conn_a.cursor()
                        cur_a.execute(
                            "SELECT username FROM zernio_accounts WHERE platform='instagram' AND is_active=TRUE AND username IS NOT NULL ORDER BY username LIMIT 1"
                        )
                        row_a = cur_a.fetchone()
                        cur_a.close()
                        conn_a.close()
                        if row_a and row_a[0]:
                            account_username = row_a[0]
                except Exception:
                    pass
            if not account_username:
                return {
                    "success": False,
                    "error": "No Instagram account connected. Connect one in Zernio first.",
                    "message": "I couldn’t post — no Instagram account is connected yet."
                }

            account_id = resolve_zernio_account_id(account_id, account_username, zernio_to_post[0])
            if not account_id:
                return {
                    "success": False,
                    "error": f"Could not resolve account '{account_username}'.",
                    "message": f"I couldn’t find Instagram @{account_username}. Check connected accounts."
                }

            print(f"📤 post_now vault_id={vault_id} account={account_id} platforms={zernio_to_post} type={content_type}")

            zernio_result = post_to_zernio_multi_platform(
                image_url=image_url,
                caption=caption or "📸 Posted via Bluesky AI Vault",
                platforms=zernio_to_post,
                scheduled_time=None,
                content_type=content_type,
                account_username=account_username
            )

            if zernio_result.get('success'):
                for p in zernio_to_post:
                    if uri:
                        mark_post_as_posted(vault_id, uri, p, zernio_result.get('post_id'), 'completed')
                results.append({
                    "platforms": zernio_to_post,
                    "success": True,
                    "message": f"✅ Posted to Instagram",
                    "post_id": zernio_result.get('post_id')
                })
            else:
                results.append({
                    "platforms": zernio_to_post,
                    "success": False,
                    "error": zernio_result.get('error')
                })

        # Build response
        success_platforms = []
        failed_platforms = []
        for r in results:
            if r.get('success'):
                success_platforms.extend(r.get('platforms', []))
            else:
                failed_platforms.extend(r.get('platforms', []))

        cap_preview = (caption or '').strip().replace('\n', ' ')
        if len(cap_preview) > 120:
            cap_preview = cap_preview[:120] + "…"
        # author is set when loading from vault; may be undefined for raw image posts
        try:
            _author = author
        except NameError:
            _author = None

        if success_platforms:
            who = f" from @{_author}" if _author else ""
            msg = (
                f"Done — I just published vault id {vault_id}{who} to Instagram"
                + (f" (@{account_username})" if account_username else "")
                + ".\n"
                + (f"Caption: “{cap_preview}”" if cap_preview else "No caption on that one.")
            )
            global _last_chat_action
            _last_chat_action = {
                "type": "post",
                "vault_id": vault_id,
                "uri": uri,
                "author": _author,
                "caption": cap_preview,
                "account_username": account_username,
                "platforms": success_platforms,
                "content_type": content_type,
                "at": datetime.now().isoformat(),
            }
        else:
            err = None
            for r in results:
                if r.get('error'):
                    err = r.get('error')
                    break
            msg = f"I couldn’t publish that post. {err or 'Unknown error.'}"

        return {
            "success": len(success_platforms) > 0,
            "platforms": success_platforms,
            "failed_platforms": failed_platforms,
            "results": results,
            "vault_id": vault_id,
            "uri": uri,
            "caption": caption,
            "account_username": account_username,
            "message": msg,
        }

    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e), "message": f"Post failed: {e}"}
























def resolve_zernio_account_id(account_id=None, account_username=None, platform='instagram'):
    """
    Resolve a Zernio account ID for a specific platform.
    Returns the account_id if found, None otherwise.
    """
    if account_id and _looks_like_zernio_id(account_id):
        # Verify it exists for the platform
        found = get_account_id_for_platform(platform, account_id)
        if found:
            return found
    
    if account_username:
        uname = account_username.replace('ig_', '').lstrip('@').strip()
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT account_id FROM zernio_accounts
                    WHERE platform = %s AND is_active = TRUE
                      AND (
                        LOWER(username) = LOWER(%s)
                        OR LOWER(display_name) = LOWER(%s)
                        OR LOWER(display_name) LIKE LOWER(%s)
                        OR LOWER(username) LIKE LOWER(%s)
                      )
                    ORDER BY username NULLS LAST LIMIT 1
                """, (platform, uname, uname, f"%{uname}%", f"%{uname}%"))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    return row[0]
        except Exception as e:
            print(f"resolve_zernio_account_id: {e}")
        
        # Fallback: scan live Zernio accounts
        for acc in get_zernio_accounts():
            if acc.get('platform') != platform:
                continue
            u = (acc.get('username') or '').lower()
            d = (acc.get('displayName') or '').lower()
            if uname.lower() in u or uname.lower() in d or u == uname.lower():
                return acc.get('_id')
    
    return get_account_id_for_platform(platform)

def get_account_id_for_platform(platform, account_id=None):
    """Get account ID for a specific platform from Zernio."""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT account_id FROM zernio_accounts WHERE account_id = %s AND platform = %s AND is_active = TRUE LIMIT 1",
                    (account_id, platform)
                )
            else:
                cur.execute(
                    "SELECT account_id FROM zernio_accounts WHERE platform = %s AND is_active = TRUE ORDER BY username NULLS LAST LIMIT 1",
                    (platform,)
                )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return row[0]
    except Exception:
        pass
    
    # Refresh from Zernio
    get_zernio_accounts()
    
    # Try again
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT account_id FROM zernio_accounts WHERE platform = %s AND is_active = TRUE ORDER BY username NULLS LAST LIMIT 1",
                (platform,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return row[0]
    except Exception:
        pass
    
    return None













def tool_post_vault_batch(
    vault_ids: list = None,
    count: int = None,
    content_type: str = "feed",
    account_id: str = None,
    account_username: str = None
) -> dict:
    """
    Post multiple vault items now.
    Pass vault_ids=[1,2] or count=2 (latest N from vault).
    """
    try:
        ids = list(vault_ids) if vault_ids else []
        if not ids and count:
            conn = get_db_connection()
            if not conn:
                return {"success": False, "error": "Database unavailable"}
            cur = conn.cursor()
            cur.execute("SELECT id FROM vault ORDER BY saved_at DESC LIMIT %s", (int(count),))
            ids = [r[0] for r in cur.fetchall()]
            cur.close()
            conn.close()
        if not ids:
            return {"success": False, "error": "Provide vault_ids or count"}

        results = []
        ok = 0
        for vid in ids:
            r = tool_post_now(
                vault_id=int(vid),
                content_type=content_type,
                account_id=account_id,
                account_username=account_username
            )
            results.append({"vault_id": vid, **r})
            if r.get('success'):
                ok += 1
            time.sleep(1.2)
        return {
            "success": ok > 0,
            "posted_count": ok,
            "failed_count": len(ids) - ok,
            "results": results,
            "message": f"Posted {ok}/{len(ids)} vault item(s) to Instagram"
        }
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def generate_random_schedule_times(start_date, end_date, total_posts, min_hours_between=2, tz=None):
    if tz is None:
        tz = pytz.timezone(SCHEDULE_TIMEZONE)
    if start_date.tzinfo is None:
        start_date = tz.localize(start_date)
    if end_date.tzinfo is None:
        end_date = tz.localize(end_date)
    now = datetime.now(tz)
    if start_date < now:
        start_date = now + timedelta(hours=1)
    total_seconds = max((end_date - start_date).total_seconds(), 3600)
    min_total = (total_posts - 1) * min_hours_between * 3600
    if total_seconds < min_total:
        end_date = start_date + timedelta(seconds=min_total)
        total_seconds = min_total
    times = []
    for _ in range(total_posts):
        offset = random.random() * total_seconds
        t = start_date + timedelta(seconds=offset)
        if t > end_date:
            t = end_date - timedelta(minutes=random.randint(1, 30))
        times.append(t)
    times.sort()
    final = []
    for t in times:
        if not final:
            final.append(t)
        else:
            gap = (t - final[-1]).total_seconds()
            if gap < min_hours_between * 3600:
                nt = final[-1] + timedelta(hours=min_hours_between, minutes=random.randint(0, 20))
                final.append(nt if nt <= end_date else final[-1] + timedelta(hours=min_hours_between))
            else:
                final.append(t)
    return final[:total_posts]


def tool_schedule_bulk(
    uris: list = None,
    count: int = None,
    period: str = "week",
    start_date: str = None,
    min_hours_between: float = 2,
    content_type: str = "feed",
    platforms: list = None,
    account_id: str = None
) -> dict:
    """
    Schedule multiple vault posts.
    Provide either `uris` (list of specific URIs) or `count` (take the latest N from vault).
    period: '24h' | 'week' | 'month'
    """
    platforms = platforms or ['instagram']
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()

        if uris:
            placeholders = ','.join(['%s'] * len(uris))
            cur.execute(f"SELECT * FROM vault WHERE uri IN ({placeholders})", uris)
        elif count:
            cur.execute("SELECT * FROM vault ORDER BY saved_at DESC LIMIT %s", (count,))
        else:
            cur.close()
            conn.close()
            return {"success": False, "error": "Provide uris or count"}

        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return {"success": False, "error": "No matching posts in vault"}

        vault_posts = []
        for row in rows:
            vault_posts.append({
                "id": row[0], "uri": row[1], "author": row[2], "display_name": row[3],
                "text": row[4], "images": row[5], "notes": row[13] if len(row) > 13 else None
            })

        tz = pytz.timezone(SCHEDULE_TIMEZONE)
        now = datetime.now(tz)
        if start_date:
            start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            if start.tzinfo is None:
                start = tz.localize(start)
            if start < now:
                start = now + timedelta(hours=1)
        else:
            start = now + timedelta(hours=1)

        period_days = {'24h': 1, 'week': 7, 'month': 30, 'year': 365}
        end = start + timedelta(days=period_days.get(period, 7))

        times = generate_random_schedule_times(start, end, len(vault_posts), min_hours_between, tz)

        scheduled = 0
        failed = []
        details = []
        for post, st in zip(vault_posts, times):
            images = post.get('images') or []
            image_url = images[0].get('url') if images else None
            if not image_url:
                failed.append({"uri": post['uri'], "error": "No image"})
                continue
            caption = f"📝 {(post.get('text') or '')[:200]}" if post.get('text') else f"Post from @{post.get('author')}"
            result = post_to_zernio(
                image_url=image_url,
                caption=caption,
                platforms=platforms,
                scheduled_time=st,
                content_type=content_type,
                account_ids=[account_id] if account_id else None
            )
            if result.get('success'):
                for p in platforms:
                    mark_post_as_posted(post['id'], post['uri'], p, result.get('post_id'), 'scheduled')
                # append note
                try:
                    conn2 = get_db_connection()
                    if conn2:
                        cur2 = conn2.cursor()
                        note = (post.get('notes') or '') + f"\n\n📅 Scheduled for {st.strftime('%Y-%m-%d %H:%M')} ({SCHEDULE_TIMEZONE})"
                        cur2.execute("UPDATE vault SET notes = %s WHERE uri = %s", (note, post['uri']))
                        conn2.commit()
                        cur2.close()
                        conn2.close()
                except Exception:
                    pass
                scheduled += 1
                details.append({"uri": post['uri'], "time": st.isoformat()})
            else:
                failed.append({"uri": post['uri'], "error": result.get('error')})
            time.sleep(0.8)

        return {
            "success": True,
            "scheduled_count": scheduled,
            "failed_count": len(failed),
            "details": details,
            "failed": failed,
            "message": f"Scheduled {scheduled} post(s) over {period}"
        }
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}






def count_zernio_api_keys():
    """Count how many Zernio API keys are configured."""
    keys = get_zernio_api_keys()
    return len(keys)


def tool_list_api_keys():
    """List all configured Zernio API keys from .env and their auto-detected accounts."""
    status = ensure_zernio_keys_loaded()
    keys = status.get('keys') or []

    if not keys:
        return {
            "success": True,
            "message": "⚠️ No Zernio API keys configured in .env\n"
                       "Set ZERNIO_API_KEY, ZERNIO_API_KEY1/ZERNIO_API_KEY2, or ZERNIO_API_KEYS=key1,key2",
            "keys": [],
            "count": 0,
            "total_accounts": 0
        }

    lines = ["🔑 Zernio API Keys (from .env):"]
    total_accounts = 0
    for key_info in keys:
        index = key_info.get('index')
        env_var = key_info.get('env_var', f'ZERNIO_API_KEY{index}')
        accounts = key_info.get('accounts', [])
        key_preview = (key_info.get('key') or '')[:16] + '…' if key_info.get('key') else 'None'
        lines.append(f"  • {env_var}: {key_preview}")
        if accounts:
            names = []
            for a in accounts:
                if isinstance(a, dict):
                    names.append(f"@{a.get('username')} ({a.get('platform', 'instagram')})")
                else:
                    names.append(str(a))
            lines.append(f"    Accounts: {', '.join(names)}")
            total_accounts += len(accounts)
        else:
            lines.append("    Accounts: (none detected — check API key validity)")
    lines.append(f"\n  Total: {len(keys)} API key(s) from .env")
    lines.append(f"  Total accounts: {total_accounts}")
    return {
        "success": True,
        "message": "\n".join(lines),
        "keys": keys,
        "count": len(keys),
        "total_accounts": total_accounts
    }

def tool_get_api_key_status(key_index: int = None):
    """Get detailed status for a specific API key or all keys."""
    keys = get_zernio_api_keys()
    
    if key_index:
        keys = [k for k in keys if k.get('index') == key_index]
        if not keys:
            return {"success": False, "error": f"API Key {key_index} not found"}
    
    results = []
    for key_info in keys:
        api_key = key_info.get('key')
        index = key_info.get('index')
        env_var = key_info.get('env_var', f'ZERNIO_API_KEY{index}')
        
        # Test the key
        status = "❓ Unknown"
        try:
            headers = get_zernio_headers_for_key(api_key)
            if headers:
                response = requests.get(
                    f"{ZERNIO_BASE_URL}/accounts",
                    headers=headers,
                    timeout=10
                )
                if response.status_code == 200:
                    accounts = response.json().get('accounts', [])
                    status = f"✅ Valid ({len(accounts)} accounts)"
                elif response.status_code == 401:
                    status = "❌ Invalid API Key"
                elif response.status_code == 429:
                    status = "⚠️ Rate Limited"
                else:
                    status = f"⚠️ Error {response.status_code}"
        except requests.exceptions.Timeout:
            status = "⏰ Timeout"
        except Exception as e:
            status = f"❌ Error: {str(e)[:30]}"
        
        results.append({
            "env_var": env_var,
            "index": index,
            "status": status,
            "accounts": key_info.get('accounts', [])
        })
    
    return {
        "success": True,
        "results": results,
        "message": "\n".join([f"  • {r['env_var']}: {r['status']}" for r in results])
    }




def tool_list_recent_posted(limit: int = 5) -> dict:
    """
    What was actually published recently (from posted_posts + vault).
    Use for: 'what did you post', 'latest published post', 'show recent posts'.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute("""
            SELECT pp.vault_id, pp.uri, pp.platform, pp.platform_post_id, pp.status, pp.posted_at,
                   v.author, v.text, v.display_name
            FROM posted_posts pp
            LEFT JOIN vault v ON v.id = pp.vault_id OR v.uri = pp.uri
            WHERE pp.status IN ('completed', 'scheduled')
            ORDER BY pp.posted_at DESC NULLS LAST
            LIMIT %s
        """, (int(limit),))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        items = []
        for r in rows:
            text = (r[7] or '').strip().replace('\n', ' ')
            if len(text) > 100:
                text = text[:100] + "…"
            items.append({
                "vault_id": r[0],
                "uri": r[1],
                "platform": r[2],
                "platform_post_id": r[3],
                "status": r[4],
                "posted_at": str(r[5]) if r[5] else None,
                "author": r[6],
                "text": text,
                "display_name": r[8],
            })

        if not items:
            msg = "I haven’t published anything yet in this session’s history — the posted log is empty."
        else:
            lines = [f"Here are the {len(items)} most recent publish(es):"]
            for i, it in enumerate(items, 1):
                when = it.get('posted_at') or 'unknown time'
                author = f"@{it['author']}" if it.get('author') else "unknown author"
                preview = f"“{it['text']}”" if it.get('text') else "(no text / image post)"
                st = it.get('status') or ''
                lines.append(
                    f"{i}. {when} · {st} · vault id {it.get('vault_id')} · {author} · {preview}"
                )
            msg = "\n".join(lines)

        return {"success": True, "count": len(items), "posted": items, "message": msg}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def tool_list_scheduled(limit: int = 20) -> dict:
    """List posts that have been scheduled with images."""
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        cur = conn.cursor()
        cur.execute("""
            SELECT uri, text, author, notes, images, display_name 
            FROM vault 
            WHERE notes LIKE %s 
            ORDER BY saved_at DESC 
            LIMIT %s
        """, ('%Scheduled for%', limit))
        rows = cur.fetchall()
        items = []
        for r in rows:
            try:
                notes = r[3] if len(r) > 3 else ''
                match = re.search(r'Scheduled for ([\d\-: ]+)', notes or '')
                
                # Extract image URLs
                images = r[4] if len(r) > 4 else []
                image_urls = []
                if images:
                    for img in images:
                        if isinstance(img, dict):
                            url = img.get('url') or img.get('thumb')
                            if url:
                                image_urls.append(url)
                        elif isinstance(img, str):
                            image_urls.append(img)
                
                items.append({
                    "uri": r[0] if len(r) > 0 else None,
                    "text": (r[1] or '')[:200] if len(r) > 1 else '',
                    "author": r[2] if len(r) > 2 else '',
                    "scheduled_for": match.group(1).strip() if match else None,
                    "notes": notes,
                    "images": image_urls,
                    "display_name": r[5] if len(r) > 5 else None,
                    "has_image": len(image_urls) > 0
                })
            except Exception as row_e:
                print(f"list_scheduled row skip: {row_e}")
        cur.close()
        conn.close()
        return {"success": True, "count": len(items), "scheduled": items}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}


# Map tool names → functions
# Map tool names → functions
TOOL_MAP = {
    "login": tool_login,
    "restore_session": tool_restore_session,
    "fetch_posts": tool_fetch_posts,
    "add_to_vault": tool_add_to_vault,
    "list_vault": tool_list_vault,
    # ===== NEW VAULT MANAGEMENT TOOLS =====
    "list_vault_by_status": tool_list_vault_by_status,
    "delete_vault_items": tool_delete_vault_items,
    "post_unposted": tool_post_unposted,
    # ===== END NEW VAULT MANAGEMENT TOOLS =====
    "remove_from_vault": tool_remove_from_vault,
    "get_status": tool_get_status,
    "list_accounts": tool_list_accounts,
    "post_now": tool_post_now,
    "post_vault_batch": tool_post_vault_batch,
    "schedule_bulk": tool_schedule_bulk,
    "list_scheduled": tool_list_scheduled,
    "list_recent_posted": tool_list_recent_posted,
    "auto_status": tool_auto_status,
    "auto_setup": tool_auto_setup,
    "auto_start": tool_auto_start,
    "auto_stop": tool_auto_stop,
    "auto_run_now": tool_auto_run_now,
    "auto_remove": tool_auto_remove,
    "list_api_keys": tool_list_api_keys,
    "get_api_key_status": tool_get_api_key_status,
    "check_zernio_key": tool_check_zernio_key,
    "delete_account": tool_delete_account_permanently,
    "delete_all_accounts": tool_delete_all_accounts_permanently,
}






































# OpenAI-compatible tool schemas (works with Gemini OpenAI endpoint)
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "login",
            "description": "Login to Bluesky with username/handle and app password. Creates a persistent session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Bluesky handle or email"},
                    "password": {"type": "string", "description": "App password"}
                },
                "required": ["username", "password"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restore_session",
            "description": "Restore a previously saved Bluesky session using only the handle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"}
                },
                "required": ["handle"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_posts",
            "description": "Fetch recent posts from a Bluesky account. Prefer media_only=true for Instagram-ready content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "actor": {"type": "string", "description": "Handle to fetch from, e.g. dailymotivator.bsky.social"},
                    "limit": {"type": "integer", "default": 20},
                    "include_reposts": {"type": "boolean", "default": False},
                    "media_only": {"type": "boolean", "default": True}
                },
                "required": ["session_id", "actor"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_vault",
            "description": "Save the most recently fetched posts into the vault. After fetch_posts, call this with session_id only — do NOT re-pass the full posts array.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Bluesky session_id from login. Uses the last fetch cached for this session."
                    },
                    "posts": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Optional. Only needed if you are not using session_id cache."
                    },
                    "handler_handle": {"type": "string"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_vault",
            "description": "Show posts currently stored in the vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 30},
                    "handler_handle": {"type": "string"}
                }
            }
        }
    },
    # ===== NEW VAULT MANAGEMENT TOOLS =====
    {
        "type": "function",
        "function": {
            "name": "list_vault_by_status",
            "description": "List vault items filtered by post status. Use 'unposted' for items not yet posted, 'posted' for already posted, 'scheduled' for scheduled, or 'all' for everything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["unposted", "posted", "scheduled", "all"],
                        "description": "Filter by post status"
                    },
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vault_items",
            "description": "PERMANENTLY delete vault items by status or all. Use with caution! This cannot be undone. ALWAYS confirm with the user before deleting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["unposted", "posted", "scheduled", "all"],
                        "description": "Delete items by status"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of vault IDs to delete"
                    },
                    "all": {
                        "type": "boolean",
                        "description": "Delete ALL vault items (requires confirmation)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "post_unposted",
            "description": "Post all unposted vault items to Instagram immediately",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_username": {
                        "type": "string",
                        "description": "Instagram account username to post to"
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Instagram account ID (optional)"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max number of items to post"
                    }
                }
            }
        }
    },
    # ===== END NEW VAULT MANAGEMENT TOOLS =====
    {
        "type": "function",
        "function": {
            "name": "remove_from_vault",
            "description": "Delete a post from the vault by its URI.",
            "parameters": {
                "type": "object",
                "properties": {"uri": {"type": "string"}},
                "required": ["uri"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Get current counts: vault size, scheduled posts, posted posts, connected accounts, active Bluesky handle.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_accounts",
            "description": "List connected Instagram accounts (via Zernio).",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "default": None, "description": "Filter by platform (instagram only)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "post_vault_batch",
            "description": "IMMEDIATELY post multiple vault items to Instagram. Use when user says 'post 2 from vault' or 'post id 1 and 2'. Pass vault_ids or count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vault_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of vault ids e.g. [1,2]"},
                    "count": {"type": "integer", "description": "Post the latest N vault items"},
                    "content_type": {"type": "string", "enum": ["feed", "story"], "default": "feed"},
                    "account_id": {"type": "string"},
                    "account_username": {"type": "string", "description": "e.g. easternfrontdaily"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_bulk",
            "description": "Schedule multiple vault posts randomly across a time window. Use count to take the latest N posts, or pass specific uris.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uris": {"type": "array", "items": {"type": "string"}},
                    "count": {"type": "integer", "description": "How many latest vault posts to schedule"},
                    "period": {"type": "string", "enum": ["24h", "week", "month"], "default": "week"},
                    "start_date": {"type": "string", "description": "ISO datetime when scheduling should begin"},
                    "min_hours_between": {"type": "number", "default": 2},
                    "content_type": {"type": "string", "enum": ["feed", "story"], "default": "feed"},
                    "platforms": {"type": "array", "items": {"type": "string"}, "default": ["instagram"]},
                    "account_id": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled",
            "description": "Show posts that are already scheduled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_status",
            "description": "Check whether the autonomous poster is running and its last result.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_setup",
            "description": "Create or update ONE autonomous pipeline. Use a unique name per Bluesky source so multiple automations can run together (e.g. name=coreiq and name=dailymotivator).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique pipeline id e.g. coreiq, dailymotivator"},
                    "source_handle": {"type": "string", "description": "Bluesky handle to watch e.g. coreiq.bsky.social"},
                    "account_username": {"type": "string", "description": "Instagram username e.g. easternfrontdaily"},
                    "account_id": {"type": "string"},
                    "poll_interval_sec": {"type": "integer", "default": 300},
                    "max_posts_per_run": {"type": "integer", "default": 2},
                    "content_type": {"type": "string", "enum": ["feed", "story"], "default": "feed"},
                    "media_only": {"type": "boolean", "default": True},
                    "include_reposts": {"type": "boolean", "default": False},
                    "bluesky_handle": {"type": "string", "description": "Your Bluesky login handle for the bot"},
                    "bluesky_app_password": {"type": "string"},
                    "enabled": {"type": "boolean"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_start",
            "description": "Enable and start autonomous pipelines. Optionally pass name to start only one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Optional pipeline name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_stop",
            "description": "Stop autonomous pipelines. Optionally pass name to stop only one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Optional pipeline name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_run_now",
            "description": "Run autonomous cycle(s) immediately (fetch new → vault → post).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Optional pipeline name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "auto_remove",
            "description": "PERMANENTLY DELETE a pipeline by name. Use when user says remove/delete pipeline X. Do NOT use for stop/disable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Pipeline name to delete e.g. scorpio, coreiq, dailymotivator"
                    }
                },
                "required": ["name"]
            }
        }
    },
    # ===== API KEY MANAGEMENT FUNCTIONS =====
    {
        "type": "function",
        "function": {
            "name": "list_api_keys",
            "description": "REQUIRED when user asks about API keys / Zernio keys / how many keys. Reads .env and lists every ZERNIO_API_KEY with auto-detected accounts. Do NOT use list_accounts for key questions.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_api_key_status",
            "description": "Get detailed status for a specific Zernio API key index from .env (1, 2, …). Tests validity and lists accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key_index": {"type": "integer", "description": "The API key index (1, 2, 3, etc.)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_zernio_key",
            "description": "REQUIRED when user pastes a Zernio API key (sk_...) or says 'check this key' / 'accounts for this key'. Queries Zernio with THAT key and lists all accounts on it. Pass the full key string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "api_key": {
                        "type": "string",
                        "description": "Full Zernio API key, e.g. sk_5ac94ab83bd78a..."
                    },
                    "save_to_db": {
                        "type": "boolean",
                        "default": True,
                        "description": "Save discovered accounts into the local DB"
                    }
                },
                "required": ["api_key"]
            }
        }
    },
    # ===== UPDATED: Multi-platform post_now =====
    {
        "type": "function",
        "function": {
            "name": "post_now",
            "description": "REQUIRED for any social media publish request. IMMEDIATELY posts one vault item to Instagram (only supported platform). Use when user says post now, post to Instagram, post id N, post this image, or confirms a post. Prefer vault_id. Never refuse posting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vault_id": {"type": "integer", "description": "Vault row id from list_vault (preferred)"},
                    "uri": {"type": "string", "description": "Vault post URI if id unknown"},
                    "image_url": {"type": "string"},
                    "caption": {"type": "string"},
                    "content_type": {"type": "string", "enum": ["feed", "story"], "default": "feed"},
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["instagram"]},
                        "default": ["instagram"],
                        "description": "Platforms to post to"
                    },
                    "account_username": {"type": "string", "description": "Instagram username e.g. easternfrontdaily (required)"}
                }
            }
        }
    },
    # ===== ACCOUNT DELETION TOOLS =====
    {
        "type": "function",
        "function": {
            "name": "delete_account",
            "description": "PERMANENTLY delete a connected account from the database. Use ONLY when user explicitly says 'delete permanently', 'remove forever', 'erase account', or 'delete account' with strong intent. This removes vault posts and posted history too. Cannot be undone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_identifier": {
                        "type": "string",
                        "description": "Username or display_name to permanently delete, e.g. 'easternfrontdaily'"
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Zernio account ID (24-char hex) if known"
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["instagram"],
                        "default": "instagram"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_all_accounts",
            "description": "PERMANENTLY delete ALL accounts from the database. Requires confirmation parameter 'YES_DELETE_ALL'. Use ONLY when user explicitly says 'delete all accounts permanently' or similar with clear intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["instagram"],
                        "description": "Optional: delete only accounts from this platform"
                    },
                    "confirm": {
                        "type": "string",
                        "description": "Must be 'YES_DELETE_ALL' to confirm deletion"
                    }
                }
            }
        }
    }
]


















SYSTEM_PROMPT = """You are the AI assistant for Bluesky AI Vault.

This service only does: Bluesky fetch → vault → Instagram (via Zernio).
Facebook and Threads are NOT supported. Do not mention them or offer to connect them.




ACCOUNT DELETION (PERMANENT - USE WITH CAUTION):
- "delete account @username permanently" → call delete_account(account_identifier="username")
- "remove forever" / "erase account" / "delete permanently" → delete_account()
- "delete all accounts permanently" → FIRST confirm, then call delete_all_accounts(confirm="YES_DELETE_ALL")
- ⚠️ PERMANENT means: removes account, vault posts, and posted history. CANNOT BE UNDONE.
- ALWAYS confirm with the user before deleting if they seem unsure.
- Use list_accounts() first if the user isn't clear which account to delete.
- After deletion, the account can be re-added by refreshing accounts from Zernio API keys.



VAULT MANAGEMENT COMMANDS:
- "list unposted" or "show unposted" → Call list_vault_by_status(status="unposted")
- "list posted" or "show posted" → Call list_vault_by_status(status="posted")  
- "list scheduled" or "show scheduled" → Call list_vault_by_status(status="scheduled")
- "list all vault" or "show all vault" → Call list_vault_by_status(status="all")
- "post unposted" → Call post_unposted()
- "post count 5" → Call post_unposted(limit=5)
- "delete unposted" → Call delete_vault_items(status="unposted")
- "delete posted" → Call delete_vault_items(status="posted")
- "delete scheduled" → Call delete_vault_items(status="scheduled")
- "delete all vault" → Call delete_vault_items(all=True) (⚠️ Requires confirmation: "YES_DELETE_ALL")
- "delete vault id 1,2,3" → Call delete_vault_items(ids=[1,2,3])





CRITICAL — API KEYS vs ACCOUNTS (do not confuse them):
- "API keys" / "Zernio keys" / "how many keys" → ALWAYS call list_api_keys() (reads .env).
  Never answer this with list_accounts. Keys and accounts are different things.
- "Accounts" / "which Instagram" / "connected accounts" → call list_accounts().
- User PASTES a key (sk_...) or says "check this key / accounts for this key" →
  ALWAYS call check_zernio_key(api_key="sk_...") with the exact key they provided.
  List only the accounts on THAT key. Do not invent accounts.

WHEN USER ASKS about API keys (examples: "how many api keys", "list keys"):
1. Call list_api_keys() — do NOT call list_accounts.
2. Report the real count from .env and accounts per key.

WHEN USER PROVIDES A KEY:
- Example: "check sk_5ac94ab..." or just pastes sk_...
- Call check_zernio_key(api_key="<full key>")
- Reply with validity + every account username/platform/id on that key.

Do NOT invent key counts or account lists.

PLATFORMS:
- Instagram (via Zernio) — destination for posts; accounts come from Zernio API keys in .env
- Bluesky (AT Protocol) — source only: login/fetch posts into the vault (not a posting target in this app)

ACCOUNT HELP:
- Connected Instagram accounts: call list_accounts()
- Bluesky: "Login with [handle] and [app-password]" then fetch posts
- For "how many API keys" use list_api_keys() — NOT list_accounts

HARD RULE — Posting:
- You CAN post to Instagram when Zernio accounts are connected
- Default (and only) platform is Instagram
- Never offer Facebook, Threads, or multi-platform "post everywhere"

When the user gives a clear actionable request, call the right tools. Do not only pretend.
When the request is vague (e.g. "add a pipeline"), ask clarifying questions first — do not call tools until you have the details.

Core workflow:
1. login / restore_session (Bluesky — needed to fetch)
2. fetch_posts(session_id, actor, limit)
3. add_to_vault(session_id=...) to save last fetch
4. post_now / post_vault_batch for immediate Instagram posts
5. schedule_bulk for delayed Instagram posts
6. auto_setup + auto_start for hands-free Bluesky → Instagram
7. auto_remove to permanently delete a pipeline

CRITICAL — stop vs remove:
- "Stop auto" / "stop pipeline X" → auto_stop (disables, keeps config)
- "Remove pipeline X" / "delete pipeline scorpio" → auto_remove(name="scorpio") (deletes forever)
- Never use auto_stop when user says remove/delete. Never use auto_remove when user says stop.

CRITICAL — Posting:
- Post to Instagram through Zernio. Account usernames are in Context.
- When user says "post now", "post id 2", "post the first 2", "post this to Instagram", "yes" → call post_now or post_vault_batch with platforms=["instagram"].
- Do NOT only call list_vault when the user already asked to post.
- Prefer vault_id (integer from list_vault). For multiple: post_vault_batch(vault_ids=[1,2], account_username="...").
- If the user wants to post a vault image and did not specify which, list_vault once then post_now with the chosen vault_id.
- Default content_type = "feed".

PLATFORM EXAMPLES:
- "Post id 5 to Instagram" → post_now(vault_id=5, platforms=["instagram"], account_username="<ig_username>")
- "Post id 5 to Facebook/Threads" → "Only Instagram is supported. I can post id 5 to Instagram if you want."

AUTONOMY (multiple pipelines supported):
- Each Bluesky source is its own pipeline with a unique name (auto-named from the handle).
- To run TWO sources at once, call auto_setup TWICE with different source_handle values, then auto_start.
  Example: auto_setup(name="dailymotivator", source_handle="dailymotivator.bsky.social", account_username="easternfrontdaily", enabled=true)
           auto_setup(name="coreiq", source_handle="coreiq.bsky.social", account_username="easternfrontdaily", enabled=true)
- auto_status lists ALL pipelines.
- auto_remove(name="scorpio") permanently deletes that pipeline.
- Prefer the existing logged-in Bluesky session for fetching.

ADDING A NEW PIPELINE (conversational — critical):
- If the user says "add a pipeline" WITHOUT full details → DO NOT call any tools. Ask in plain chat.
- Account names are in Context — use the connected Instagram accounts.
- Collect only missing fields one step at a time. Defaults if user skips: poll_interval_sec=300, max_posts_per_run=1, content_type=feed, enabled=true.
- Minimum required: source_handle (Bluesky) + account_username (Instagram account).
- As soon as BOTH are known, call auto_setup once.
- Example: user said source=spacecowboy17.bsky.social then destination=easternfrontdaily → immediately:
  auto_setup(name="spacecowboy17", source_handle="spacecowboy17.bsky.social", account_username="easternfrontdaily", enabled=true)
- After auto_setup succeeds, briefly confirm the pipeline in plain English.

REPLY STYLE (critical):
- NEVER paste raw JSON, tool dumps, or {"success":...} into the user-facing reply.
- Always summarize tool results in short plain English.
- For list_accounts: say the usernames only, not the full JSON.
- Prefer a short friendly conversation over calling tools for vague requests.
- Be HONEST about what's connected. Only Instagram posting is supported.

Other rules:
- "save them / save to vault" → add_to_vault(session_id=...) only.
- Status questions → get_status or list_vault.
- Scheduling → schedule_bulk(count=N, period="week", platforms=["instagram"]).
- Be concise. Report tool results honestly. Never invent success.
- Timezone: Africa/Nairobi.
"""










# Lightweight chat context cache (avoids Zernio network + heavy DB on every message)
_chat_context_cache = {"ts": 0, "bits": []}
_CHAT_CONTEXT_TTL = 45  # seconds


def _quick_chat_context(session_id=None):
    """
    Fast context for Gemini: memory + cheap DB only.
    Does NOT call Zernio or refresh accounts (that was a major latency source).
    """
    global _chat_context_cache
    now = time.time()
    bits = []

    if session_id and session_id in sessions:
        bits.append(f"Active Bluesky session_id: {session_id} (handle: {sessions[session_id].get('handle')})")
    else:
        bits.append("No active Bluesky session — user may need login / restore_session.")

    # Reuse cached account/status snippet if fresh
    if now - _chat_context_cache["ts"] < _CHAT_CONTEXT_TTL and _chat_context_cache["bits"]:
        bits.extend(_chat_context_cache["bits"])
        return bits

    cached = []
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM vault")
            vault_n = cur.fetchone()[0]
            cur.execute(
                "SELECT username FROM zernio_accounts WHERE platform='instagram' AND is_active=TRUE ORDER BY username NULLS LAST LIMIT 8"
            )
            ig = [r[0] for r in cur.fetchall() if r[0]]
            cur.execute(
                "SELECT name, enabled, source_handle, account_username FROM auto_config ORDER BY name LIMIT 10"
            )
            pipes = cur.fetchall()
            cur.close()
            conn.close()
            cached.append(f"Vault: {vault_n} posts")
            cached.append(
                "Instagram accounts: " + (", ".join(f"@{u}" for u in ig) if ig else "none in DB")
            )
            if pipes:
                plines = []
                for name, en, src, acct in pipes:
                    plines.append(f"{'[ON]' if en else '[OFF]'} {name}: @{src or '?'} → {acct or '?'}")
                cached.append("Pipelines: " + "; ".join(plines))
            else:
                cached.append("Pipelines: none")
    except Exception as e:
        print(f"quick context: {e}")

    cached.append("Post only to Instagram via post_now / post_vault_batch.")
    _chat_context_cache = {"ts": now, "bits": cached}
    bits.extend(cached)
    return bits


def execute_tool(name, arguments, session_id=None):
    """Execute a tool by name with arguments."""
    fn = TOOL_MAP.get(name)
    if not fn:
        return {"success": False, "error": f"Unknown tool: {name}"}
    try:
        # arguments may arrive as string
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        
        # Handle tools that need session_id
        if name in ['fetch_posts', 'add_to_vault']:
            return fn(**arguments, session_id=session_id) if session_id else fn(**arguments)
        
        # Special handling for tools that need special processing
        if name == 'login':
            return fn(arguments.get('username'), arguments.get('password'))
        
        if name == 'restore_session':
            return fn(arguments.get('handle'))
        
        if name == 'fetch_posts':
            if not session_id and arguments.get('session_id'):
                session_id = arguments.get('session_id')
            if not session_id:
                return {"success": False, "error": "Login first"}
            return fn(
                session_id,
                arguments.get('actor'),
                limit=int(arguments.get('limit') or 20),
                media_only=bool(arguments.get('media_only', True)),
                include_reposts=bool(arguments.get('include_reposts', False))
            )
        
        if name == 'add_to_vault':
            posts = []
            if session_id and session_id in sessions:
                posts = sessions[session_id].get('_last_fetched') or []
            return fn(posts, handler_handle=sessions.get(session_id, {}).get('_last_actor'))
        
        if name == 'list_vault':
            return fn(limit=int(arguments.get('limit') or 30))
        
        # ===== NEW VAULT MANAGEMENT TOOLS =====
        if name == 'list_vault_by_status':
            return fn(
                status=arguments.get('status', 'all'),
                limit=int(arguments.get('limit', 50)),
                offset=int(arguments.get('offset', 0))
            )
        
        if name == 'delete_vault_items':
            # Require confirmation for "delete all"
            if arguments.get('all'):
                confirm = arguments.get('confirm')
                if confirm != 'YES_DELETE_ALL':
                    return {
                        "success": False, 
                        "error": "Confirmation required",
                        "message": "⚠️ This will permanently delete ALL vault items. Reply with 'YES_DELETE_ALL' to confirm."
                    }
            return fn(
                ids=arguments.get('ids'),
                status=arguments.get('status'),
                all=arguments.get('all', False)
            )
        
        if name == 'post_unposted':
            return fn(
                account_username=arguments.get('account_username'),
                account_id=arguments.get('account_id'),
                limit=int(arguments.get('limit', 10))
            )
        # ===== END NEW VAULT MANAGEMENT TOOLS =====
        
        if name == 'post_now':
            return fn(
                vault_id=arguments.get('vault_id'),
                uri=arguments.get('uri'),
                image_url=arguments.get('image_url'),
                caption=arguments.get('caption'),
                content_type=arguments.get('content_type', 'feed'),
                platforms=arguments.get('platforms', ['instagram']),
                account_id=arguments.get('account_id'),
                account_username=arguments.get('account_username')
            )
        
        if name == 'post_vault_batch':
            return fn(
                vault_ids=arguments.get('vault_ids'),
                count=arguments.get('count'),
                content_type=arguments.get('content_type', 'feed'),
                account_id=arguments.get('account_id'),
                account_username=arguments.get('account_username')
            )
        
        if name == 'schedule_bulk':
            return fn(
                uris=arguments.get('uris'),
                count=arguments.get('count'),
                period=arguments.get('period', 'week'),
                start_date=arguments.get('start_date'),
                min_hours_between=arguments.get('min_hours_between', 2),
                content_type=arguments.get('content_type', 'feed'),
                platforms=arguments.get('platforms', ['instagram']),
                account_id=arguments.get('account_id')
            )
        
        if name == 'list_accounts':
            return fn(platform=arguments.get('platform'))
        
        if name == 'auto_setup':
            return fn(
                name=arguments.get('name'),
                source_handle=arguments.get('source_handle'),
                account_username=arguments.get('account_username'),
                account_id=arguments.get('account_id'),
                poll_interval_sec=arguments.get('poll_interval_sec', 300),
                max_posts_per_run=arguments.get('max_posts_per_run', 2),
                content_type=arguments.get('content_type', 'feed'),
                media_only=bool(arguments.get('media_only', True)),
                include_reposts=bool(arguments.get('include_reposts', False)),
                bluesky_handle=arguments.get('bluesky_handle'),
                bluesky_app_password=arguments.get('bluesky_app_password'),
                enabled=arguments.get('enabled', True)
            )
        
        if name == 'auto_start':
            return fn(name=arguments.get('name'))
        
        if name == 'auto_stop':
            return fn(name=arguments.get('name'))
        
        if name == 'auto_run_now':
            return fn(name=arguments.get('name'))
        
        if name == 'auto_remove':
            return fn(name=arguments.get('name'))
        
        if name == 'check_zernio_key':
            return fn(
                api_key=arguments.get('api_key'),
                save_to_db=arguments.get('save_to_db', True)
            )
        
        if name == 'get_api_key_status':
            return fn(key_index=arguments.get('key_index'))
        
        if name == 'delete_account':
            return fn(
                account_identifier=arguments.get('account_identifier'),
                account_id=arguments.get('account_id'),
                platform=arguments.get('platform', 'instagram')
            )
        
        if name == 'delete_all_accounts':
            return fn(
                platform=arguments.get('platform'),
                confirm=arguments.get('confirm')
            )
        
        # For any other tool, just call it directly
        return fn(**arguments)
        
    except TypeError as e:
        return {"success": False, "error": f"Bad arguments for {name}: {e}"}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}















# ============================================================
# UPDATED /api/chat ENDPOINT WITH IMAGE SUPPORT
# ============================================================

@app.route('/api/chat', methods=['POST'])
def chat():
    """Handle chat messages with optional image upload"""
    # Check if this is a multipart form upload (has image)
    if request.files and 'image' in request.files:
        return handle_chat_with_image()
    else:
        return handle_chat_json()


def handle_chat_json():
    """Handle JSON chat requests (existing functionality)"""
    data = request.json or {}
    user_message = (data.get('message') or '').strip()
    history = data.get('history') or []
    session_id = data.get('session_id')
    chat_key = data.get('chat_key') or str(uuid.uuid4())

    if not user_message:
        return jsonify({"success": False, "error": "Empty message"}), 400

    # ---- HARD PRE-ROUTE: keys / full account list (do not trust Gemini) ----
    lower_msg = user_message.lower()

    # User pasted a Zernio key (sk_...) → check THAT key's accounts
    sk_match = re.search(r'(sk_[A-Za-z0-9]{20,})', user_message)
    if sk_match or (
        any(p in lower_msg for p in ('check this key', 'check my key', 'check key',
                                     'accounts for this key', 'accounts on this key',
                                     'accounts related to this', 'accounts for my key'))
        and 'sk_' in user_message
    ):
        key = sk_match.group(1) if sk_match else None
        if not key:
            # try after = in ZERNIO_API_KEY2=sk_...
            m2 = re.search(r'=\s*(sk_[A-Za-z0-9]+)', user_message)
            key = m2.group(1) if m2 else None
        if key:
            result = tool_check_zernio_key(api_key=key, save_to_db=True)
            return jsonify({
                "success": True,
                "reply": result.get('message') or str(result),
                "tool_results": [{"name": "check_zernio_key", "result": result}],
                "chat_key": chat_key,
                "session_id": session_id,
                "used_pre_route": True,
            })

    wants_keys = any(p in lower_msg for p in (
        'api key', 'api keys', 'zernio key', 'zernio keys',
        'how many key', 'list key', 'what keys', 'keys do we have',
        'keys configured', 'env key', '.env key', 'check them all',
        'zernio_api_key'
    ))
    wants_all_accounts = any(p in lower_msg for p in (
        'all the accounts', 'all accounts', 'accounts list',
        'list accounts', 'accounts related', 'accounts for those',
        'accounts on the key', 'accounts on each', 'every account'
    ))

    if wants_keys and not wants_all_accounts:
        result = tool_list_api_keys()
        reply = result.get('message') or "Could not read Zernio API keys from .env."
        return jsonify({
            "success": True,
            "reply": reply,
            "tool_results": [{"name": "list_api_keys", "result": result}],
            "chat_key": chat_key,
            "session_id": session_id,
            "used_pre_route": True,
        })

    if wants_all_accounts or (wants_keys and 'account' in lower_msg):
        result = tool_list_accounts()
        reply = result.get('message') or "Could not list accounts."
        return jsonify({
            "success": True,
            "reply": reply,
            "tool_results": [{"name": "list_accounts", "result": result}],
            "chat_key": chat_key,
            "session_id": session_id,
            "used_pre_route": True,
        })

    # Build messages for the model (lean context = faster Gemini)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    context_bits = _quick_chat_context(session_id)
    if context_bits:
        messages.append({"role": "system", "content": "Context:\n" + "\n".join(context_bits)})

    # Fewer history turns → less prompt tokens → faster
    for h in history[-6:]:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            # Cap very long past messages
            content = h['content']
            if len(content) > 800:
                content = content[:800] + "…"
            messages.append({"role": h['role'], "content": content})

    messages.append({"role": "user", "content": user_message})

    # ---- Call Gemini (single round-trip preferred) ----
    response_data, err = call_gemini(messages, tools=TOOLS_SCHEMA, max_tokens=700, timeout=35)

    if err:
        print(f"⚠️ Gemini error: {err}")
        # Fallback: simple keyword routing so the service still works without AI
        fallback = simple_fallback(user_message, session_id)
        reply = fallback
        if "No GEMINI_API_KEYS" not in err and "not configured" not in err:
            reply = f"{fallback}\n\n(AI error: {err})"

        # If login succeeded in fallback, pull the newest session_id
        new_sid = session_id
        if '✅' in fallback and 'Session ID:' in fallback:
            m = re.search(r'Session ID:\s*(\S+)', fallback)
            if m:
                new_sid = m.group(1)

        return jsonify({
            "success": True,
            "reply": reply,
            "chat_key": chat_key,
            "session_id": new_sid,
            "used_fallback": True,
            "warning": err
        })

    choice = response_data['choices'][0]['message']
    tool_calls = choice.get('tool_calls') or []

    # Execute tools if any — reply from local summary (skip 2nd Gemini call = ~2x faster)
    tool_results = []
    if tool_calls:
        for tc in tool_calls:
            name = tc['function']['name']
            args = tc['function'].get('arguments', '{}')
            print(f"🔧 Tool call: {name}({args})")
            result = execute_tool(name, args)
            tool_results.append({"name": name, "result": result})

        # Prefer tool's own message field; otherwise format_tool_summary
        reply = format_tool_summary(tool_results)
        # If the first model also returned text, prepend briefly (rare with tool_choice auto)
        extra = (choice.get('content') or '').strip()
        if extra and len(extra) < 200 and extra not in reply:
            reply = f"{extra}\n\n{reply}" if reply else extra
    else:
        reply = choice.get('content') or "I'm not sure what to do with that."

    return jsonify({
        "success": True,
        "reply": reply,
        "tool_results": tool_results,
        "chat_key": chat_key,
        "session_id": session_id
    })


# ============================================================
# HANDLE CHAT WITH IMAGE UPLOAD
# ============================================================
def handle_chat_with_image():
    """Handle chat requests with image upload"""
    try:
        message = request.form.get('message', '')
        history = json.loads(request.form.get('history', '[]')) if request.form.get('history') else []
        session_id = request.form.get('session_id')
        chat_key = request.form.get('chat_key') or str(uuid.uuid4())
        
        # Get the uploaded image
        image_file = request.files.get('image')
        if not image_file:
            return jsonify({
                "success": False, 
                "error": "No image uploaded",
                "reply": "Please upload an image first."
            }), 400
        
        # Save image temporarily
        import tempfile
        import shutil
        from werkzeug.utils import secure_filename
        
        temp_dir = tempfile.mkdtemp()
        filename = secure_filename(image_file.filename or 'image.jpg')
        temp_path = os.path.join(temp_dir, filename)
        image_file.save(temp_path)
        
        # Check what the user wants to do with the image
        message_lower = message.lower()
        
        # Get connected account
        account_id = get_first_instagram_account()
        
        # If no account, try to get one from Zernio
        if not account_id:
            account_id = get_account_id_for_platform('instagram')
        
        # Caption = user text unless it's only a command phrase
        caption = message.strip()
        if re.match(
            r'^(post|publish|share|upload|schedule|save)\b.*(image|photo|pic|instagram|ig|vault)?\s*$',
            caption,
            re.I
        ) or not caption:
            caption = '📸 Posted via AI Vault'

        # Route by intent — NEVER hand Instagram post off to Gemini (it wrongly refuses)
        if any(word in message_lower for word in ['schedule', 'later', 'delay', 'tomorrow']):
            time_match = re.search(r'(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})', message)
            schedule_time = time_match.group(1) if time_match else None
            result = schedule_image_post(temp_path, caption, account_id, schedule_time, session_id)
        elif any(word in message_lower for word in ['save', 'vault', 'store', 'keep']) and 'post' not in message_lower:
            result = save_image_to_vault(temp_path, caption, account_id, session_id)
        else:
            # Default + explicit post/publish/share/upload/instagram → post now
            result = post_image_to_instagram(temp_path, caption, account_id, session_id)
        
        # Clean up temp files
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        return jsonify(result)
        
    except Exception as e:
        print(f"Image chat error: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False, 
            "error": str(e),
            "reply": f"❌ Error processing image: {str(e)}"
        }), 500


# ============================================================
# IMAGE HELPER FUNCTIONS - ADD THESE
# ============================================================

def get_first_instagram_account():
    """Get the first connected Instagram account"""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT account_id FROM zernio_accounts 
                WHERE platform = 'instagram' AND is_active = TRUE 
                ORDER BY created_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return row[0]
    except Exception as e:
        print(f"Error getting account: {e}")
    return None


def upload_media_to_zernio_from_file(file_path):
    """Upload image file to Zernio"""
    try:
        # Get presigned URL
        presign_payload = {
            "filename": os.path.basename(file_path),
            "contentType": "image/jpeg"
        }
        
        presign_response = requests.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=get_zernio_headers(),
            json=presign_payload,
            timeout=30
        )
        
        if presign_response.status_code not in [200, 201]:
            print(f"Presign failed: {presign_response.text}")
            return None
        
        data = presign_response.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')
        
        if not upload_url or not public_url:
            return None
        
        # Upload the file
        with open(file_path, 'rb') as f:
            upload_response = requests.put(
                upload_url,
                data=f,
                headers={'Content-Type': 'image/jpeg'},
                timeout=60
            )
        
        if upload_response.status_code not in [200, 201, 204]:
            return None
        
        return public_url
    except Exception as e:
        print(f"Upload error: {e}")
        return None


def post_image_to_instagram(image_path, caption, account_id, session_id):
    """Post image to Instagram via Zernio"""
    try:
        if not account_id:
            return {
                "success": False,
                "reply": "❌ No Instagram account connected. Please connect an account in Zernio first.",
                "tool_results": [{"name": "post_image", "result": {"error": "No account"}}],
                "session_id": session_id
            }
        
        # Upload to Zernio
        media_url = upload_media_to_zernio_from_file(image_path)
        if not media_url:
            return {
                "success": False,
                "reply": "❌ Failed to upload image to Zernio. Please try again.",
                "tool_results": [{"name": "post_image", "result": {"error": "Upload failed"}}],
                "session_id": session_id
            }
        
        # Post to Instagram
        result = post_to_zernio(
            image_url=media_url,
            caption=caption or "📸 Posted via AI Vault",
            platforms=['instagram'],
            scheduled_time=None,
            content_type='feed',
            account_ids=[account_id]
        )
        
        if result.get('success'):
            return {
                "success": True,
                "reply": f"✅ Image posted to Instagram successfully!\n\nCaption: {caption or '(no caption)'}",
                "tool_results": [{"name": "post_image", "result": {"success": True, "post_id": result.get('post_id')}}],
                "session_id": session_id
            }
        else:
            return {
                "success": False,
                "reply": f"❌ Failed to post: {result.get('error', 'Unknown error')}",
                "tool_results": [{"name": "post_image", "result": {"error": result.get('error')}}],
                "session_id": session_id
            }
    except Exception as e:
        return {
            "success": False,
            "reply": f"❌ Error posting: {str(e)}",
            "tool_results": [{"name": "post_image", "result": {"error": str(e)}}],
            "session_id": session_id
        }


def schedule_image_post(image_path, caption, account_id, schedule_time, session_id):
    """Schedule image post"""
    try:
        if not account_id:
            return {
                "success": False,
                "reply": "❌ No Instagram account connected.",
                "tool_results": [{"name": "schedule_image", "result": {"error": "No account"}}],
                "session_id": session_id
            }
        
        media_url = upload_media_to_zernio_from_file(image_path)
        if not media_url:
            return {
                "success": False,
                "reply": "❌ Failed to upload image.",
                "tool_results": [{"name": "schedule_image", "result": {"error": "Upload failed"}}],
                "session_id": session_id
            }
        
        # Parse schedule time
        scheduled_datetime = None
        if schedule_time:
            try:
                scheduled_datetime = datetime.strptime(schedule_time, '%Y-%m-%d %H:%M')
            except:
                pass
        
        result = post_to_zernio(
            image_url=media_url,
            caption=caption or "📸 Scheduled via AI Vault",
            platforms=['instagram'],
            scheduled_time=scheduled_datetime,
            content_type='feed',
            account_ids=[account_id]
        )
        
        if result.get('success'):
            return {
                "success": True,
                "reply": f"✅ Image scheduled successfully!{f' at {schedule_time}' if schedule_time else ''}",
                "tool_results": [{"name": "schedule_image", "result": {"success": True}}],
                "session_id": session_id
            }
        else:
            return {
                "success": False,
                "reply": f"❌ Failed to schedule: {result.get('error', 'Unknown error')}",
                "tool_results": [{"name": "schedule_image", "result": {"error": result.get('error')}}],
                "session_id": session_id
            }
    except Exception as e:
        return {
            "success": False,
            "reply": f"❌ Error scheduling: {str(e)}",
            "tool_results": [{"name": "schedule_image", "result": {"error": str(e)}}],
            "session_id": session_id
        }


def save_image_to_vault(image_path, caption, account_id, session_id):
    """Save image to vault"""
    try:
        import base64
        
        # Read image as base64
        with open(image_path, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
        
        conn = get_db_connection()
        if not conn:
            return {
                "success": False,
                "reply": "❌ Database connection failed.",
                "tool_results": [{"name": "save_to_vault", "result": {"error": "DB error"}}],
                "session_id": session_id
            }
        
        cur = conn.cursor()
        
        # Create a vault entry
        vault_uri = f"upload_{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO vault (uri, author, text, images, saved_at, notes)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
            RETURNING id
        """, (
            vault_uri,
            "AI Upload",
            caption or "📸 Uploaded via AI",
            Json([{'url': f'data:image/jpeg;base64,{image_data}'}]),
            f"Uploaded via AI on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        ))
        
        result = cur.fetchone()
        conn.commit()
        vault_id = result[0] if result else None
        
        cur.close()
        conn.close()
        
        return {
            "success": True,
            "reply": f"✅ Image saved to vault successfully!\n\nCaption: {caption or '(no caption)'}",
            "tool_results": [{"name": "save_to_vault", "result": {"success": True, "id": vault_id, "uri": vault_uri}}],
            "session_id": session_id
        }
    except Exception as e:
        return {
            "success": False,
            "reply": f"❌ Error saving to vault: {str(e)}",
            "tool_results": [{"name": "save_to_vault", "result": {"error": str(e)}}],
            "session_id": session_id
        }


def process_image_with_ai(image_path, message, history, session_id, chat_key):
    """Process image with AI - let Gemini decide what to do"""
    try:
        # Build prompt for AI
        ai_messages = [
            {"role": "system", "content": f"""You are an AI assistant for a social media management tool. 
            The user has uploaded an image and said: "{message}"

            You have these options:
            1. Post to Instagram (if user says "post" or "publish")
            2. Schedule for later (if user says "schedule")
            3. Save to vault (if user says "save" or mentions "vault")
            4. Just acknowledge the image

            Respond in a friendly, helpful way. If the user didn't specify what to do, ask them what they'd like to do with the image.

            The image is available for posting. You can help the user post, schedule, or save it.
            """}
        ]
        
        for h in history[-6:]:
            if h.get('role') in ('user', 'assistant') and h.get('content'):
                ai_messages.append({"role": h['role'], "content": h['content']})
        
        ai_messages.append({"role": "user", "content": f"[Image uploaded: {os.path.basename(image_path)}] {message}"})
        
        # Call Gemini
        response_data, err = call_gemini(ai_messages)
        
        if err or not response_data:
            # Fallback: ask user what to do
            return {
                "success": True,
                "reply": f"📸 I see you've uploaded an image. Would you like me to:\n\n- Post it to Instagram\n- Schedule it for later\n- Save it to your vault\n\nJust tell me what you'd like to do!",
                "tool_results": [],
                "session_id": session_id
            }
        
        reply = response_data['choices'][0]['message'].get('content', "I see you uploaded an image. What would you like to do with it?")
        
        return {
            "success": True,
            "reply": reply,
            "tool_results": [],
            "session_id": session_id
        }
    except Exception as e:
        return {
            "success": False,
            "reply": f"❌ Error processing image with AI: {str(e)}",
            "tool_results": [],
            "session_id": session_id
        }



































def format_tool_summary(tool_results):
    """Human-readable summary with proper vault formatting."""
    parts = []
    for tr in tool_results:
        name = tr.get('name')
        r = tr.get('result') or {}
        
        if not r.get('success'):
            parts.append(f"❌ {name}: {r.get('error') or r.get('message') or 'failed'}")
            continue
        
        # ===== HANDLE VAULT LISTING TOOLS =====
        if name in ['list_vault', 'list_vault_by_status']:
            items = r.get('vault') or []
            count = r.get('count') or len(items)
            status_filter = r.get('status_filter', 'all')
            
            if count == 0:
                status_display = status_filter if status_filter != 'all' else ''
                parts.append(f"📦 No {status_display} posts in vault." if status_display else "📦 Your vault is empty right now.")
            else:
                status_emoji = {'unposted': '⬜', 'posted': '✅', 'scheduled': '⏳', 'all': '📦'}
                emoji = status_emoji.get(status_filter, '📦')
                status_label = status_filter if status_filter != 'all' else ''
                
                lines = [f"{emoji} Vault has **{count}** {status_label} post(s):"]
                for i, item in enumerate(items[:5], 1):
                    text = (item.get('text') or '').strip()
                    if len(text) > 70:
                        text = text[:70] + "..."
                    if not text:
                        text = "(image only)"
                    img_count = len(item.get('images') or [])
                    img_text = f" 📸{img_count}" if img_count > 0 else ""
                    author = f" @{item.get('author', '?')}"
                    status_icon = {
                        'unposted': '⬜', 
                        'posted': '✅', 
                        'scheduled': '⏳'
                    }.get(item.get('post_status', 'unposted'), '')
                    lines.append(f"  {i}. {status_icon} id={item.get('id')} {text}{img_text}{author}")
                
                if len(items) > 5:
                    lines.append(f"  ...and {len(items) - 5} more")
                
                parts.append("\n".join(lines))
            continue
        
        # ===== HANDLE SCHEDULED LIST =====
        if name == 'list_scheduled':
            items = r.get('scheduled') or []
            if not items:
                parts.append("📅 No scheduled posts.")
            else:
                lines = [f"📅 Scheduled ({r.get('count')}):"]
                for it in items:
                    img_indicator = " 📸" if it.get('has_image') else ""
                    lines.append(f"• {it.get('scheduled_for')} — {(it.get('text') or '')[:60]}{img_indicator}")
                parts.append("\n".join(lines))
            continue
        
        # ===== HANDLE POST UNPOSTED =====
        if name == 'post_unposted':
            posted_count = r.get('posted_count', 0)
            total = r.get('total', 0)
            if posted_count > 0:
                parts.append(f"✅ Posted {posted_count}/{total} unposted items to Instagram")
            else:
                errors = r.get('errors', [])
                if errors:
                    parts.append(f"❌ Failed to post: {', '.join(errors[:3])}")
                else:
                    parts.append("📦 No unposted items to post")
            continue
        
        # ===== HANDLE DELETE VAULT ITEMS =====
        if name == 'delete_vault_items':
            deleted = r.get('deleted_count', 0)
            if deleted > 0:
                parts.append(f"🗑️ Permanently deleted {deleted} item(s) from vault")
            else:
                parts.append("📦 No items to delete")
            continue
        
        # ===== HANDLE POST NOW =====
        if name == 'post_now':
            if r.get('success'):
                platforms = r.get('platforms', ['instagram'])
                parts.append(f"✅ Posted to {', '.join(platforms)} successfully!")
                if r.get('message'):
                    parts.append(r.get('message'))
            else:
                parts.append(f"❌ Post failed: {r.get('error', 'Unknown error')}")
            continue
        
        # ===== HANDLE ACCOUNTS =====
        if name == 'list_accounts':
            accounts = r.get('accounts') or []
            if not accounts:
                parts.append("📱 No connected Instagram accounts")
            else:
                lines = [f"📱 Instagram accounts ({len(accounts)}):"]
                for a in accounts[:10]:
                    label = a.get('display_name') or a.get('username') or a.get('account_id')
                    lines.append(f"  • @{label}")
                if len(accounts) > 10:
                    lines.append(f"  ...and {len(accounts) - 10} more")
                parts.append("\n".join(lines))
            continue
        
        # ===== HANDLE AUTO STATUS =====
        if name == 'auto_status':
            running = r.get('running', False)
            pipelines = r.get('pipelines', [])
            enabled_count = len([p for p in pipelines if p.get('enabled')])
            status = "🟢 RUNNING" if running else "🔴 STOPPED"
            parts.append(f"🤖 Auto pilot: {status} · {enabled_count} pipeline(s) enabled")
            if pipelines:
                for p in pipelines[:5]:
                    state = '🟢' if p.get('enabled') else '🔴'
                    src = p.get('source_handle', '?')
                    dest = p.get('account_username', '?')
                    interval = p.get('poll_interval_sec', 300)
                    parts.append(f"  {state} {p.get('name')}: @{src} → @{dest} ({interval}s)")
            continue
        
        # ===== HANDLE API KEYS =====
        if name == 'list_api_keys':
            keys = r.get('keys', [])
            total_accounts = r.get('total_accounts', 0)
            parts.append(f"🔑 {len(keys)} Zernio API key(s) configured · {total_accounts} account(s)")
            for k in keys[:3]:
                env_var = k.get('env_var', f"ZERNIO_API_KEY{k.get('index', '?')}")
                key_preview = k.get('key', '')[:16] + '…' if k.get('key') else 'None'
                accounts = k.get('accounts', [])
                acc_names = [f"@{a.get('username')}" for a in accounts if a.get('username')]
                acc_str = ', '.join(acc_names) if acc_names else 'no accounts'
                parts.append(f"  • {env_var}: {key_preview} → {acc_str}")
            if len(keys) > 3:
                parts.append(f"  ...and {len(keys) - 3} more")
            continue
        
        # ===== HANDLE CHECK ZERNIO KEY =====
        if name == 'check_zernio_key':
            if r.get('valid'):
                count = r.get('count', 0)
                parts.append(f"✅ Valid key · {count} account(s)")
                accounts = r.get('accounts', [])
                for a in accounts[:5]:
                    parts.append(f"  • @{a.get('username')} ({a.get('platform')})")
                if len(accounts) > 5:
                    parts.append(f"  ...and {len(accounts) - 5} more")
            else:
                parts.append(f"❌ {r.get('message', 'Invalid key')}")
            continue
        
        # ===== HANDLE STATUS =====
        if name == 'get_status':
            vault = r.get('vault_count', 0)
            posted = r.get('posted_count', 0)
            scheduled = r.get('scheduled_count', 0)
            accounts = r.get('accounts_count', 0)
            handle = r.get('active_handle', 'None')
            lines = [
                f"📊 Status:",
                f"  • Vault: {vault} posts",
                f"  • Posted: {posted}",
                f"  • Scheduled: {scheduled}",
                f"  • Instagram accounts: {accounts}",
                f"  • Bluesky session: @{handle}" if handle and handle != 'None' else "  • Bluesky session: None"
            ]
            parts.append("\n".join(lines))
            continue
        
        # ===== FALLBACK =====
        # For any other tool, show a brief summary
        if r.get('message'):
            parts.append(r['message'])
        else:
            # Extract key values for a summary
            summary_keys = ['posted_count', 'saved', 'scheduled_count', 'count', 'handle', 'deleted_count']
            short = {k: v for k, v in r.items() if k in summary_keys and v is not None}
            if short:
                parts.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in short.items()))
            else:
                parts.append(f"✅ {name} completed")
    
    return "\n".join(parts) if parts else "Done."























def simple_fallback(msg, session_id):
    """Keyword router so core actions work even when Gemini is offline."""
    lower = msg.lower().strip()

    # --- LOGIN ---
    if lower.startswith('login ') or 'login with' in lower:
        # patterns: "login with handle and password" | "login handle password"
        m = re.search(
            r'login(?:\s+with)?\s+([^\s]+)\s+(?:and\s+)?(.+)',
            msg.strip(),
            re.IGNORECASE
        )
        if m:
            username = m.group(1).strip().rstrip(',')
            password = m.group(2).strip()
            # strip trailing punctuation
            password = password.rstrip('.,!')
            result = tool_login(username, password)
            if result.get('success'):
                return f"✅ {result.get('message')}\nSession ID: {result.get('session_id')}"
            return f"❌ Login failed: {result.get('error')}"
        return "Format: Login with <handle> and <app-password>"

    # --- RESTORE ---
    if 'restore' in lower and ('session' in lower or '@' in lower or '.bsky' in lower):
        m = re.search(r'@?([a-zA-Z0-9._-]+\.bsky\.social|[a-zA-Z0-9._-]+)', msg)
        if m:
            result = tool_restore_session(m.group(1))
            return result.get('message') or result.get('error') or str(result)

    # --- PASTED ZERNIO KEY ---
    sk = re.search(r'(sk_[A-Za-z0-9]{20,})', msg)
    if sk:
        return tool_check_zernio_key(api_key=sk.group(1)).get('message') or str(
            tool_check_zernio_key(api_key=sk.group(1))
        )

    # --- API KEYS (before STATUS — "how many api keys" must not hit status) ---
    if any(w in lower for w in ('api key', 'api keys', 'zernio key', 'zernio keys', 'how many key')):
        r = tool_list_api_keys()
        return r.get('message') or str(r)

    # --- STATUS ---
    if any(w in lower for w in ('status', 'how many', "what's in", 'counts')):
        r = tool_get_status()
        if r.get('success'):
            return r.get('message', str(r))
        return str(r)

    # --- LIST VAULT ---
    if 'vault' in lower and any(w in lower for w in ('list', 'show', 'what')):
        r = tool_list_vault(limit=10)
        if not r.get('success'):
            return r.get('error', str(r))
        items = r.get('vault') or []
        if not items:
            return "Vault is empty."
        lines = [f"Vault ({r.get('count')} items):"]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. @{it.get('author')}: {(it.get('text') or '')[:80]}")
        return "\n".join(lines)

    # --- LIST SCHEDULED ---
    if 'scheduled' in lower:
        r = tool_list_scheduled()
        if not r.get('success'):
            return r.get('error', str(r))
        items = r.get('scheduled') or []
        if not items:
            return "No scheduled posts."
        lines = [f"Scheduled ({r.get('count')}):"]
        for it in items:
            lines.append(f"• {it.get('scheduled_for')} — {(it.get('text') or '')[:60]}")
        return "\n".join(lines)

    # --- ACCOUNTS ---
    if 'account' in lower and 'api' not in lower:
        r = tool_list_accounts()
        if not r.get('success'):
            return r.get('error', str(r))
        accs = r.get('accounts') or []
        if not accs:
            return "No connected Instagram accounts."
        return "Accounts:\n" + "\n".join(f"• @{a.get('label')} ({a.get('account_id')})" for a in accs)





    # --- FETCH (basic) ---
    if 'fetch' in lower:
        m = re.search(r'@?([a-zA-Z0-9._-]+\.bsky\.social|[a-zA-Z0-9._-]+)', msg)
        limit_m = re.search(r'(\d+)\s*posts?', lower)
        limit = int(limit_m.group(1)) if limit_m else 15
        
        # Auto-detect session if not provided
        if not session_id or session_id not in sessions:
            try:
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT session_id, session_string, handle 
                        FROM sessions 
                        WHERE expires_at > CURRENT_TIMESTAMP 
                        ORDER BY last_used_at DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    cur.close()
                    conn.close()
                    if row:
                        sid, session_string, handle = row
                        if sid not in sessions:
                            client = Client()
                            client.login(session_string=session_string)
                            sessions[sid] = {
                                'client': client,
                                'handle': handle,
                                'session_string': session_string
                            }
                        session_id = sid
                        print(f"✅ Auto-restored session for @{handle}")
            except Exception as e:
                print(f"Auto-restore failed: {e}")
        
        # If still no session, try to use any existing session in memory
        if not session_id or session_id not in sessions:
            if sessions:
                session_id = list(sessions.keys())[0]
                print(f"✅ Using existing session: {session_id}")
            else:
                return "Not logged in. Say: Login with <handle> and <app-password>"
        
        if not m:
            # Try to extract handle from the message
            words = msg.split()
            for word in words:
                if '.bsky.social' in word or ('@' in word and '.' in word):
                    m = re.search(r'@?([a-zA-Z0-9._-]+\.bsky\.social|[a-zA-Z0-9._-]+)', word)
                    break
            if not m:
                return "Say: Fetch 15 posts from @handle"
        
        actor = m.group(1)
        if '.' not in actor:
            actor = actor + '.bsky.social'
        r = tool_fetch_posts(session_id, actor, limit=limit)
        if not r.get('success'):
            return f"❌ {r.get('error')}"
        posts = r.get('posts') or []
        lines = [f"Fetched {len(posts)} posts from @{actor}:"]
        for i, p in enumerate(posts[:8], 1):
            media = f" [{len(p.get('images') or [])} img]" if p.get('images') else ""
            lines.append(f"{i}. {(p.get('text') or '')[:70]}{media}")
        if len(posts) > 8:
            lines.append(f"...and {len(posts)-8} more")
        lines.append("\nSay “save them to vault” to store them.")
        # stash posts on the session object for a follow-up "save"
        if session_id in sessions:
            sessions[session_id]['_last_fetched'] = posts
            sessions[session_id]['_last_actor'] = actor
        return "\n".join(lines)











    # --- SAVE TO VAULT ---
    if any(w in lower for w in ('save', 'add to vault', 'vault them')):
        if not session_id or session_id not in sessions:
            return "Not logged in / no recent fetch. Fetch posts first."
        posts = sessions[session_id].get('_last_fetched') or []
        if not posts:
            return "No recent fetch to save. Fetch posts first."
        actor = sessions[session_id].get('_last_actor')
        r = tool_add_to_vault(posts, handler_handle=actor)
        return r.get('message') or r.get('error') or str(r)

    # --- SCHEDULE ---
    if 'schedule' in lower:
        count_m = re.search(r'(\d+)\s*posts?', lower)
        count = int(count_m.group(1)) if count_m else 10
        period = 'week'
        if '24h' in lower or 'day' in lower:
            period = '24h'
        elif 'month' in lower:
            period = 'month'
        r = tool_schedule_bulk(count=count, period=period)
        return r.get('message') or r.get('error') or str(r)

    # --- POST BY ID ---
    id_m = re.search(r'post\s+(?:id\s+)?(\d+)', lower)
    if id_m or re.search(r'\bid\s*(\d+)\b', lower):
        vid = int(id_m.group(1) if id_m else re.search(r'\bid\s*(\d+)\b', lower).group(1))
        content_type = 'story' if 'story' in lower else 'feed'
        acct = None
        if 'eastern' in lower:
            acct = 'easternfrontdaily'
        elif 'serpent' in lower:
            acct = 'serpent_sniper1'
        result = tool_post_now(vault_id=vid, content_type=content_type, account_username=acct)
        return result.get('message') or result.get('error') or str(result)

    # --- POST THIS IMAGE / POST TO INSTAGRAM (no explicit id) ---
    if re.search(r'post\s+(this\s+)?(image|photo|pic)?\s*(to\s+)?(instagram|ig)?', lower) or \
       ('instagram' in lower and 'post' in lower) or \
       ('post' in lower and 'vault' in lower) or 'post now' in lower or 'post them' in lower:
        count_m = re.search(r'(\d+)\s*posts?', lower)
        count = int(count_m.group(1)) if count_m else 1
        content_type = 'story' if 'story' in lower else 'feed'
        acct = None
        if 'eastern' in lower:
            acct = 'easternfrontdaily'
        elif 'serpent' in lower:
            acct = 'serpent_sniper1'
        if count > 1:
            result = tool_post_vault_batch(count=count, content_type=content_type, account_username=acct)
        else:
            r = tool_list_vault(limit=5)
            items = r.get('vault') or []
            if not items:
                return (
                    "No vault items to post. "
                    "Upload an image with the 📷 button, pick an account, type a caption, then click Post — "
                    "or fetch Bluesky posts and save them to the vault first."
                )
            # Prefer an item that has images
            chosen = None
            for it in items:
                imgs = it.get('images')
                if imgs:
                    chosen = it
                    break
            chosen = chosen or items[0]
            result = tool_post_now(
                vault_id=chosen.get('id'),
                content_type=content_type,
                account_username=acct
            )
        return result.get('message') or result.get('error') or str(result)

    # --- AUTO REMOVE ---
    if any(w in lower for w in ('remove pipeline', 'delete pipeline', 'remove auto', 'delete auto')):
        m = re.search(r'(?:remove|delete)\s+(?:pipeline|auto)\s+([a-zA-Z0-9._-]+)', msg, re.I)
        if m:
            return tool_auto_remove(m.group(1)).get('message') or str(tool_auto_remove(m.group(1)))
        return "Say: Remove pipeline <name>  e.g. Remove pipeline scorpio"

    # --- AUTO ---
    if any(w in lower for w in ('auto status', 'autopilot', 'auto pilot')):
        return tool_auto_status().get('message', str(tool_auto_status()))
    if any(w in lower for w in ('stop auto', 'auto stop', 'disable auto')):
        return tool_auto_stop().get('message')
    if any(w in lower for w in ('start auto', 'auto start', 'go autonomous', 'work without me')):
        return str(tool_auto_start())
    if 'auto run' in lower or 'run auto' in lower:
        return str(tool_auto_run_now())

    return (
        "I can: login, fetch, save to vault, post now (by id), schedule, auto pilot, status.\n"
        "Examples:\n"
        "  Login with handle and app-password\n"
        "  Post id 2 to easternfrontdaily\n"
        "  Auto setup watch zorrito post to easternfrontdaily every 5 minutes\n"
        "  Start auto / Stop auto / Auto status\n"
        "  Remove pipeline scorpio"
    )

# ============================================================
# IMAGE UPLOAD → POST / SCHEDULE / VAULT (UI endpoints)
# ============================================================

def data_url_to_jpeg_bytes(image_data: str):
    """Convert data URL or raw base64 to JPEG BytesIO."""
    try:
        raw = image_data
        if ',' in raw and str(raw).strip().lower().startswith('data:'):
            raw = raw.split(',', 1)[1]
        binary = base64.b64decode(raw)
        img = Image.open(BytesIO(binary))
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        out = BytesIO()
        img.save(out, format='JPEG', quality=92, optimize=True)
        out.seek(0)
        return out, None
    except Exception as e:
        traceback.print_exc()
        return None, f"Invalid image data: {e}"


def upload_media_bytes_to_zernio(image_bytes, content_type='feed'):
    """Upload in-memory JPEG to Zernio; returns (public_url, error)."""
    try:
        fixed = fix_image_for_story(image_bytes) if content_type == 'story' else fix_image_for_feed(image_bytes)
        presign = requests.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=get_zernio_headers(),
            json={"filename": "upload.jpg", "contentType": "image/jpeg"},
            timeout=30
        )
        if presign.status_code not in (200, 201):
            return None, f"Presign failed: {presign.status_code} {presign.text[:200]}"
        data = presign.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')
        if not upload_url or not public_url:
            return None, f"Missing uploadUrl/publicUrl: {data}"
        fixed.seek(0)
        up = requests.put(
            upload_url,
            headers={'Content-Type': 'image/jpeg'},
            data=fixed,
            verify=False,
            timeout=60
        )
        if up.status_code not in (200, 201, 204):
            return None, f"Upload PUT failed: {up.status_code}"
        return public_url, None
    except Exception as e:
        traceback.print_exc()
        return None, str(e)


def parse_schedule_time(schedule_time: str):
    """Parse 'YYYY-MM-DD HH:MM' in Africa/Nairobi → aware datetime, or None."""
    if not schedule_time or not str(schedule_time).strip():
        return None
    tz = pytz.timezone(SCHEDULE_TIMEZONE)
    s = str(schedule_time).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt
    except Exception:
        return None


@app.route('/api/post-now/accounts', methods=['GET'])
def api_post_now_accounts():
    """Accounts for the image-upload dropdown."""
    platform = request.args.get('platform', 'instagram')
    return jsonify(tool_list_accounts(platform))


@app.route('/api/post-now', methods=['POST'])
def api_post_now_image():
    """
    Post to Instagram now.
    Preferred: { vault_id, account_id?, caption?, content_type? }
    Or raw upload: { image_data, caption, account_id, content_type, platform }
    """
    data = request.json or {}
    vault_id = data.get('vault_id')
    image_data = data.get('image_data') or data.get('image')
    caption = (data.get('caption') or '').strip()
    account_id = data.get('account_id')
    account_username = data.get('account_username')
    content_type = data.get('content_type') or 'feed'
    platform = (data.get('platform') or 'instagram').lower()

    if platform != 'instagram':
        return jsonify({"success": False, "error": f"Platform '{platform}' not supported yet (Instagram only)"}), 400

    # --- Path A: post from vault (easy for AI + UI) ---
    if vault_id is not None and str(vault_id).strip() != '':
        try:
            vid = int(vault_id)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "vault_id must be an integer"}), 400
        result = tool_post_now(
            vault_id=vid,
            caption=caption or "",
            content_type=content_type,
            account_id=account_id,
            account_username=account_username
        )
        return jsonify(result), (200 if result.get('success') else 500)

    # --- Path B: raw image_data → upload → post (and optionally already in vault) ---
    if not image_data:
        return jsonify({"success": False, "error": "Provide vault_id or image_data"}), 400

    resolved = resolve_instagram_account_id(account_id, account_username)
    if not resolved:
        return jsonify({"success": False, "error": "Could not resolve Instagram account. Connect one in Zernio."}), 400

    jpeg, err = data_url_to_jpeg_bytes(image_data)
    if not jpeg:
        return jsonify({"success": False, "error": err or "Invalid image"}), 400

    public_url, up_err = upload_media_bytes_to_zernio(jpeg, content_type)
    if not public_url:
        return jsonify({"success": False, "error": up_err or "Upload failed"}), 500

    result = post_to_zernio(
        image_url=public_url,
        caption=caption or '📸 Posted via AI Vault',
        platforms=['instagram'],
        scheduled_time=None,
        content_type=content_type,
        account_ids=[resolved]
    )
    if result.get('success'):
        result['message'] = f"Posted to Instagram ({content_type})"
        result['caption'] = caption or '📸 Posted via AI Vault'
        result['account_id'] = resolved
    return jsonify(result), (200 if result.get('success') else 500)


@app.route('/api/post-now/schedule', methods=['POST'])
def api_post_now_schedule():
    """Schedule an uploaded image + caption. schedule_time: 'YYYY-MM-DD HH:MM' or null."""
    data = request.json or {}
    image_data = data.get('image_data') or data.get('image')
    caption = (data.get('caption') or '').strip() or '📸 Scheduled via AI Vault'
    account_id = data.get('account_id')
    account_username = data.get('account_username')
    content_type = data.get('content_type') or 'feed'
    platform = (data.get('platform') or 'instagram').lower()
    schedule_time_raw = data.get('schedule_time')

    if not image_data:
        return jsonify({"success": False, "error": "image_data is required"}), 400
    if platform != 'instagram':
        return jsonify({"success": False, "error": f"Platform '{platform}' not supported yet"}), 400

    resolved = resolve_instagram_account_id(account_id, account_username)
    if not resolved:
        return jsonify({"success": False, "error": "Could not resolve Instagram account"}), 400

    scheduled_for = parse_schedule_time(schedule_time_raw) if schedule_time_raw else None

    jpeg, err = data_url_to_jpeg_bytes(image_data)
    if not jpeg:
        return jsonify({"success": False, "error": err or "Invalid image"}), 400

    public_url, up_err = upload_media_bytes_to_zernio(jpeg, content_type)
    if not public_url:
        return jsonify({"success": False, "error": up_err or "Upload failed"}), 500

    result = post_to_zernio(
        image_url=public_url,
        caption=caption,
        platforms=['instagram'],
        scheduled_time=scheduled_for,
        content_type=content_type,
        account_ids=[resolved]
    )
    if result.get('success'):
        when = scheduled_for.strftime('%Y-%m-%d %H:%M') if scheduled_for else 'ASAP'
        result['message'] = f"Scheduled to Instagram ({content_type}) for {when}"
        result['caption'] = caption
        result['account_id'] = resolved
        result['scheduled_for'] = when
    return jsonify(result), (200 if result.get('success') else 500)













@app.route('/api/vault/add-image', methods=['POST'])
def api_vault_add_image():
    """Save an uploaded image into the vault."""
    data = request.json or {}
    image_data = data.get('image_data') or data.get('image')
    caption = (data.get('caption') or '').strip() or '📸 Saved from AI Vault'
    platform = data.get('platform') or 'instagram'
    account_id = data.get('account_id')

    if not image_data:
        return jsonify({"success": False, "error": "image_data is required"}), 400

    public_url = None
    jpeg, err = data_url_to_jpeg_bytes(image_data)
    if jpeg:
        public_url, _ = upload_media_bytes_to_zernio(jpeg, 'feed')

    image_entry = {
        "url": public_url or image_data,
        "thumb": public_url or image_data,
        "alt": caption[:120]
    }
    uri = f"local:upload:{uuid.uuid4()}"

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "error": "Database unavailable"}), 500
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO vault (uri, author, display_name, text, images, likes, reposts, replies, created_at, handler_handle, notes)
            VALUES (%s, %s, %s, %s, %s, 0, 0, 0, %s, %s, %s)
            RETURNING id
        ''', (
            uri,
            'upload',
            'Manual upload',
            caption,
            Json([image_entry]),
            datetime.now().isoformat(),
            account_id or 'manual',
            f"Uploaded via UI · platform={platform}"
        ))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "success": True,
            "vault_id": row[0] if row else None,
            "uri": uri,
            "message": "Image saved to vault",
            "caption": caption
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# BASIC REST (optional direct access)
# ============================================================

@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify(tool_get_status())


@app.route('/api/accounts', methods=['GET'])
def api_accounts():
    return jsonify(tool_list_accounts(request.args.get('platform', 'instagram')))


@app.route('/api/auto/status', methods=['GET'])
def api_auto_status():
    return jsonify(tool_auto_status())


@app.route('/api/auto/start', methods=['POST'])
def api_auto_start():
    return jsonify(tool_auto_start())


@app.route('/api/auto/stop', methods=['POST'])
def api_auto_stop():
    return jsonify(tool_auto_stop())


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')






@app.route('/api/accounts/delete', methods=['POST'])
def api_delete_account_permanently():
    """Permanently delete an account from the database."""
    data = request.json or {}
    account_identifier = data.get('account_identifier')
    account_id = data.get('account_id')
    platform = data.get('platform', 'instagram')
    
    result = tool_delete_account_permanently(account_identifier, account_id, platform)
    return jsonify(result), (200 if result.get('success') else 400)

@app.route('/api/accounts/delete-all', methods=['POST'])
def api_delete_all_accounts():
    """Permanently delete ALL accounts from the database."""
    data = request.json or {}
    platform = data.get('platform')
    confirm = data.get('confirm')
    
    result = tool_delete_all_accounts_permanently(platform, confirm)
    return jsonify(result), (200 if result.get('success') else 400)














# Vercel serverless compatibility - ADD THIS AT THE VERY BOTTOM OF THE FILE
# This allows Vercel to import the app as a module
handler = app

if __name__ == '__main__':
    print("🚀 Bluesky AI Vault starting...")
    
    # Check Gemini keys
    if GEMINI_API_KEYS:
        print(f"✅ Gemini keys loaded: {len(GEMINI_API_KEYS)} (round-robin)")
        for i, k in enumerate(GEMINI_API_KEYS, 1):
            print(f"   {i}. …{k[-8:]}")
    else:
        print("⚠️  No GEMINI_API_KEYS — using keyword fallback only")

    # Check Database URL
    if not DATABASE_URL:
        print("⚠️  DATABASE_URL not set — database features will not work")
    else:
        print("✅ Database URL configured")

    # Always check .env for Zernio API keys and sync ALL accounts into DB
    zernio_status = ensure_zernio_keys_loaded()
    if not zernio_status.get('success'):
        print("⚠️  Autonomous pipelines / Instagram posting will fail until keys are set in .env")
    else:
        try:
            synced = refresh_all_zernio_accounts()
            print(f"✅ Synced {len(synced) if synced else 0} Zernio account(s) into database")
        except Exception as e:
            print(f"⚠️ Account sync: {e}")

    # Resume auto pilot if any pipeline was left enabled
    try:
        enabled = [c for c in _list_auto_configs() if c.get('enabled')]
        if enabled:
            start_result = start_auto_pilot()
            if start_result.get('success'):
                print(f"🤖 Auto pilot resumed ({len(enabled)} pipeline(s) enabled)")
            else:
                print(f"🤖 Auto pilot NOT started: {start_result.get('message') or start_result.get('error')}")
        else:
            print("🤖 Auto pilot idle (enable via chat)")
    except Exception as e:
        print(f"Auto pilot init: {e}")
    
    # Get port from environment (Vercel sets this automatically)
    port = int(os.environ.get('PORT', 10000))
    
    # Run the app
    print(f"🚀 Server starting on port {port}")
    app.run(debug=False, host='0.0.0.0', port=port)