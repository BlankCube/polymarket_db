"""Simple JWT auth. Will be replaced with X/Twitter OAuth later."""

import jwt
import bcrypt
import psycopg2
from datetime import datetime, timedelta

from config import DB_PARAMS, JWT_SECRET, JWT_ALGORITHM, JWT_TOKEN_EXPIRE_DAYS


def _get_conn():
    return psycopg2.connect(**DB_PARAMS)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, username: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=JWT_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def register(username: str, password: str, display_name: str = None) -> dict:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return {"error": "Username already taken"}
            pw_hash = hash_password(password)
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (%s, %s, %s) RETURNING id",
                (username, pw_hash, display_name or username)
            )
            user_id = cur.fetchone()[0]
        conn.commit()
        token = create_token(user_id, username)
        return {"token": token, "user_id": user_id, "username": username}
    finally:
        conn.close()


def login(username: str, password: str) -> dict:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, password_hash, display_name FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                return {"error": "Invalid username or password"}
            user_id, uname, pw_hash, display = row
            if not verify_password(password, pw_hash):
                return {"error": "Invalid username or password"}
            token = create_token(user_id, uname)
            return {"token": token, "user_id": user_id, "username": uname, "display_name": display}
    finally:
        conn.close()


def get_user_from_request(request) -> dict | None:
    """Extract user from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return decode_token(auth[7:])
    return None
