from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

app = FastAPI(title="MiniCloud Identity", version="0.1.0")

DATA_DIR = Path(os.environ.get("IDENTITY_DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "identity.db"
JWT_SECRET = os.environ.get("IDENTITY_JWT_SECRET", "change-me-minicloud-identity")
JWT_ISSUER = os.environ.get("IDENTITY_JWT_ISSUER", "minicloud-identity")
JWT_AUDIENCE = os.environ.get("IDENTITY_JWT_AUDIENCE", "minicloud")
JWT_EXP_MINUTES = int(os.environ.get("IDENTITY_JWT_EXP_MINUTES", "60"))
PBKDF2_ITERATIONS = int(os.environ.get("IDENTITY_PBKDF2_ITERATIONS", "210000"))
BOOTSTRAP_ADMIN_USER = os.environ.get("IDENTITY_BOOTSTRAP_ADMIN_USER", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("IDENTITY_BOOTSTRAP_ADMIN_PASSWORD", "admin")
ENC_KEY_RAW = os.environ.get("IDENTITY_ENCRYPTION_KEY", "")

_auth_scheme = HTTPBearer(auto_error=False)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    username: str
    groups: list[str]
    scopes: list[str]


class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class GroupSummary(BaseModel):
    name: str


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)
    groups: list[str] = Field(default_factory=list)


class UserSummary(BaseModel):
    username: str
    is_active: bool
    groups: list[str]


class UserGroupsUpdate(BaseModel):
    groups: list[str]


class PermissionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    description: str = ""


class PermissionSummary(BaseModel):
    name: str
    description: str


class UserPermissionsUpdate(BaseModel):
    permissions: list[str]


class GroupPermissionsUpdate(BaseModel):
    permissions: list[str]


class SecretUpsert(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1)
    description: str = ""


class SecretMeta(BaseModel):
    name: str
    description: str
    created_by: str
    created_at: str
    updated_at: str


class SecretValue(SecretMeta):
    value: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw.encode("ascii"))


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(digest)}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_raw, salt_raw, hash_raw = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iter_raw)
        salt = _b64d(salt_raw)
        expected = _b64d(hash_raw)
    except Exception:
        return False

    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(got, expected)


def _encryption_key() -> bytes:
    if ENC_KEY_RAW:
        key = _b64d(ENC_KEY_RAW)
        if len(key) != 32:
            raise RuntimeError("IDENTITY_ENCRYPTION_KEY must decode to 32 bytes")
        return key

    # Demo fallback: derive a stable key from JWT secret.
    return hashlib.sha256(JWT_SECRET.encode("utf-8")).digest()


def _encrypt_secret(plain: str) -> tuple[bytes, bytes]:
    key = _encryption_key()
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ciphertext = aes.encrypt(nonce, plain.encode("utf-8"), None)
    return nonce, ciphertext


def _decrypt_secret(nonce: bytes, ciphertext: bytes) -> str:
    key = _encryption_key()
    aes = AESGCM(key)
    plain = aes.decrypt(nonce, ciphertext, None)
    return plain.decode("utf-8")


def _user_groups(conn: sqlite3.Connection, username: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT g.name
        FROM groups g
        JOIN user_groups ug ON ug.group_id = g.id
        JOIN users u ON u.id = ug.user_id
        WHERE u.username = ?
        ORDER BY g.name
        """,
        (username,),
    ).fetchall()
    return [str(r["name"]) for r in rows]


def _user_direct_permissions(conn: sqlite3.Connection, username: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT p.name
        FROM permissions p
        JOIN user_permissions up ON up.permission_id = p.id
        JOIN users u ON u.id = up.user_id
        WHERE u.username = ?
        """,
        (username,),
    ).fetchall()
    return {str(r["name"]) for r in rows}


def _group_permissions(conn: sqlite3.Connection, username: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT p.name
        FROM permissions p
        JOIN group_permissions gp ON gp.permission_id = p.id
        JOIN groups g ON g.id = gp.group_id
        JOIN user_groups ug ON ug.group_id = g.id
        JOIN users u ON u.id = ug.user_id
        WHERE u.username = ?
        """,
        (username,),
    ).fetchall()
    return {str(r["name"]) for r in rows}


def _effective_permissions(conn: sqlite3.Connection, username: str) -> list[str]:
    perms = _user_direct_permissions(conn, username) | _group_permissions(conn, username)
    return sorted(perms)


def _create_token(username: str, groups: list[str], scopes: list[str]) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=JWT_EXP_MINUTES)
    payload = {
        "sub": username,
        "groups": groups,
        "scope": " ".join(scopes),
        "scp": scopes,
        "permissions": scopes,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}") from e


def _current_user(creds: HTTPAuthorizationCredentials | None = Depends(_auth_scheme)) -> dict[str, Any]:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    claims = _decode_token(creds.credentials)
    raw_scopes = claims.get("scp")
    scopes: list[str] = []
    if isinstance(raw_scopes, list):
        scopes = [str(s) for s in raw_scopes if str(s)]
    else:
        scope_str = str(claims.get("scope", "")).strip()
        if scope_str:
            scopes = [p for p in scope_str.split(" ") if p]
    return {
        "username": str(claims.get("sub", "")),
        "groups": list(claims.get("groups", [])),
        "scopes": scopes,
    }


def _require_admin(user: dict[str, Any] = Depends(_current_user)) -> dict[str, Any]:
    if "admins" not in user["groups"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin group required")
    return user


@app.on_event("startup")
def _startup() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_groups (
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, group_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_permissions (
                user_id INTEGER NOT NULL,
                permission_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, permission_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(permission_id) REFERENCES permissions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_permissions (
                group_id INTEGER NOT NULL,
                permission_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, permission_id),
                FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
                FOREIGN KEY(permission_id) REFERENCES permissions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS secrets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
        )

        # Bootstrap admin group + user for first login.
        now = _now_iso()
        conn.execute("INSERT OR IGNORE INTO groups(name, created_at) VALUES(?, ?)", ("admins", now))
        conn.execute("INSERT OR IGNORE INTO groups(name, created_at) VALUES(?, ?)", ("operators", now))
        conn.execute("INSERT OR IGNORE INTO groups(name, created_at) VALUES(?, ?)", ("viewers", now))

        conn.execute(
            "INSERT OR IGNORE INTO permissions(name, description, created_at) VALUES(?, ?, ?)",
            ("minicloud:*", "Full access for administrators", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO permissions(name, description, created_at) VALUES(?, ?, ?)",
            ("minicloud:workflow:run:*", "Run any workflow", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO permissions(name, description, created_at) VALUES(?, ?, ?)",
            ("minicloud:workflow:retrigger:*", "Re-trigger any workflow run", now),
        )

        conn.execute(
            "INSERT OR IGNORE INTO users(username, password_hash, is_active, created_at) VALUES(?, ?, 1, ?)",
            (BOOTSTRAP_ADMIN_USER, _hash_password(BOOTSTRAP_ADMIN_PASSWORD), now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO users(username, password_hash, is_active, created_at) VALUES(?, ?, 1, ?)",
            ("operator", _hash_password("operator"), now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO users(username, password_hash, is_active, created_at) VALUES(?, ?, 1, ?)",
            ("viewer", _hash_password("viewer"), now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_groups(user_id, group_id)
            SELECT u.id, g.id
            FROM users u, groups g
            WHERE u.username = ? AND g.name = 'admins'
            """,
            (BOOTSTRAP_ADMIN_USER,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_groups(user_id, group_id)
            SELECT u.id, g.id
            FROM users u, groups g
            WHERE u.username = 'operator' AND g.name = 'operators'
            """,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_groups(user_id, group_id)
            SELECT u.id, g.id
            FROM users u, groups g
            WHERE u.username = 'viewer' AND g.name = 'viewers'
            """,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO group_permissions(group_id, permission_id)
            SELECT g.id, p.id
            FROM groups g, permissions p
            WHERE g.name = 'admins' AND p.name = 'minicloud:*'
            """,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO group_permissions(group_id, permission_id)
            SELECT g.id, p.id
            FROM groups g, permissions p
            WHERE g.name = 'operators' AND p.name = 'minicloud:workflow:run:*'
            """,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO group_permissions(group_id, permission_id)
            SELECT g.id, p.id
            FROM groups g, permissions p
            WHERE g.name = 'operators' AND p.name = 'minicloud:workflow:retrigger:*'
            """,
        )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginBody) -> TokenResponse:
    with _db() as conn:
        row = conn.execute(
            "SELECT username, password_hash, is_active FROM users WHERE username = ?",
            (body.username,),
        ).fetchone()
        if row is None or not _verify_password(body.password, str(row["password_hash"])):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        if not bool(row["is_active"]):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")

        groups = _user_groups(conn, str(row["username"]))
        scopes = _effective_permissions(conn, str(row["username"]))
        token = _create_token(str(row["username"]), groups, scopes)
        return TokenResponse(
            access_token=token,
            expires_in=JWT_EXP_MINUTES * 60,
            username=str(row["username"]),
            groups=groups,
            scopes=scopes,
        )


@app.post("/token", response_model=TokenResponse)
def token_endpoint(form_data: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """Standard OAuth2 token endpoint."""
    return login(LoginBody(username=form_data.username, password=form_data.password))


@app.get("/auth/me")
def auth_me(user: dict[str, Any] = Depends(_current_user)) -> dict[str, Any]:
    return user


@app.get("/permissions", response_model=list[PermissionSummary])
def list_permissions(_admin: dict[str, Any] = Depends(_require_admin)) -> list[PermissionSummary]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT name, description FROM permissions ORDER BY name",
        ).fetchall()
    return [PermissionSummary(name=str(r["name"]), description=str(r["description"])) for r in rows]


@app.post("/permissions", response_model=PermissionSummary)
def create_permission(body: PermissionCreate, _admin: dict[str, Any] = Depends(_require_admin)) -> PermissionSummary:
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO permissions(name, description, created_at) VALUES(?, ?, ?)",
                (body.name, body.description, _now_iso()),
            )
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=409, detail="Permission already exists") from e
    return PermissionSummary(name=body.name, description=body.description)


@app.get("/groups", response_model=list[GroupSummary])
def list_groups(_admin: dict[str, Any] = Depends(_require_admin)) -> list[GroupSummary]:
    with _db() as conn:
        rows = conn.execute("SELECT name FROM groups ORDER BY name").fetchall()
    return [GroupSummary(name=str(r["name"])) for r in rows]


@app.post("/groups", response_model=GroupSummary)
def create_group(body: GroupCreate, _admin: dict[str, Any] = Depends(_require_admin)) -> GroupSummary:
    with _db() as conn:
        try:
            conn.execute("INSERT INTO groups(name, created_at) VALUES(?, ?)", (body.name, _now_iso()))
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=409, detail="Group already exists") from e
    return GroupSummary(name=body.name)


@app.get("/users", response_model=list[UserSummary])
def list_users(_admin: dict[str, Any] = Depends(_require_admin)) -> list[UserSummary]:
    with _db() as conn:
        users = conn.execute("SELECT username, is_active FROM users ORDER BY username").fetchall()
        out: list[UserSummary] = []
        for u in users:
            uname = str(u["username"])
            out.append(
                UserSummary(
                    username=uname,
                    is_active=bool(u["is_active"]),
                    groups=_user_groups(conn, uname),
                ),
            )
    return out


@app.post("/users", response_model=UserSummary)
def create_user(body: UserCreate, _admin: dict[str, Any] = Depends(_require_admin)) -> UserSummary:
    now = _now_iso()
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO users(username, password_hash, is_active, created_at) VALUES(?, ?, 1, ?)",
                (body.username, _hash_password(body.password), now),
            )
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=409, detail="User already exists") from e

        if body.groups:
            for name in body.groups:
                grp = conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
                if grp is None:
                    raise HTTPException(status_code=400, detail=f"Unknown group: {name}")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_groups(user_id, group_id)
                    SELECT u.id, ? FROM users u WHERE u.username = ?
                    """,
                    (int(grp["id"]), body.username),
                )

        return UserSummary(username=body.username, is_active=True, groups=_user_groups(conn, body.username))


@app.put("/users/{username}/groups", response_model=UserSummary)
def set_user_groups(
    username: str,
    body: UserGroupsUpdate,
    _admin: dict[str, Any] = Depends(_require_admin),
) -> UserSummary:
    with _db() as conn:
        user_row = conn.execute("SELECT id, is_active FROM users WHERE username = ?", (username,)).fetchone()
        if user_row is None:
            raise HTTPException(status_code=404, detail="User not found")

        group_ids: list[int] = []
        for name in body.groups:
            grp = conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
            if grp is None:
                raise HTTPException(status_code=400, detail=f"Unknown group: {name}")
            group_ids.append(int(grp["id"]))

        conn.execute("DELETE FROM user_groups WHERE user_id = ?", (int(user_row["id"]),))
        for gid in group_ids:
            conn.execute("INSERT INTO user_groups(user_id, group_id) VALUES(?, ?)", (int(user_row["id"]), gid))

        return UserSummary(
            username=username,
            is_active=bool(user_row["is_active"]),
            groups=_user_groups(conn, username),
        )


@app.get("/users/{username}/permissions", response_model=list[str])
def get_user_permissions(username: str, _admin: dict[str, Any] = Depends(_require_admin)) -> list[str]:
    with _db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        return _effective_permissions(conn, username)


@app.put("/users/{username}/permissions", response_model=list[str])
def set_user_permissions(
    username: str,
    body: UserPermissionsUpdate,
    _admin: dict[str, Any] = Depends(_require_admin),
) -> list[str]:
    with _db() as conn:
        user_row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if user_row is None:
            raise HTTPException(status_code=404, detail="User not found")

        perm_ids: list[int] = []
        for name in body.permissions:
            perm = conn.execute("SELECT id FROM permissions WHERE name = ?", (name,)).fetchone()
            if perm is None:
                raise HTTPException(status_code=400, detail=f"Unknown permission: {name}")
            perm_ids.append(int(perm["id"]))

        conn.execute("DELETE FROM user_permissions WHERE user_id = ?", (int(user_row["id"]),))
        for pid in perm_ids:
            conn.execute("INSERT INTO user_permissions(user_id, permission_id) VALUES(?, ?)", (int(user_row["id"]), pid))

        return _effective_permissions(conn, username)


@app.put("/groups/{group_name}/permissions", response_model=list[str])
def set_group_permissions(
    group_name: str,
    body: GroupPermissionsUpdate,
    _admin: dict[str, Any] = Depends(_require_admin),
) -> list[str]:
    with _db() as conn:
        group_row = conn.execute("SELECT id FROM groups WHERE name = ?", (group_name,)).fetchone()
        if group_row is None:
            raise HTTPException(status_code=404, detail="Group not found")

        perm_ids: list[int] = []
        for name in body.permissions:
            perm = conn.execute("SELECT id FROM permissions WHERE name = ?", (name,)).fetchone()
            if perm is None:
                raise HTTPException(status_code=400, detail=f"Unknown permission: {name}")
            perm_ids.append(int(perm["id"]))

        conn.execute("DELETE FROM group_permissions WHERE group_id = ?", (int(group_row["id"]),))
        for pid in perm_ids:
            conn.execute(
                "INSERT INTO group_permissions(group_id, permission_id) VALUES(?, ?)",
                (int(group_row["id"]), pid),
            )

        rows = conn.execute(
            """
            SELECT p.name
            FROM permissions p
            JOIN group_permissions gp ON gp.permission_id = p.id
            WHERE gp.group_id = ?
            ORDER BY p.name
            """,
            (int(group_row["id"]),),
        ).fetchall()
        return [str(r["name"]) for r in rows]


@app.get("/secrets", response_model=list[SecretMeta])
def list_secrets(_admin: dict[str, Any] = Depends(_require_admin)) -> list[SecretMeta]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT name, description, created_by, created_at, updated_at FROM secrets ORDER BY name",
        ).fetchall()
    return [
        SecretMeta(
            name=str(r["name"]),
            description=str(r["description"]),
            created_by=str(r["created_by"]),
            created_at=str(r["created_at"]),
            updated_at=str(r["updated_at"]),
        )
        for r in rows
    ]


@app.post("/secrets", response_model=SecretMeta)
def upsert_secret(body: SecretUpsert, admin: dict[str, Any] = Depends(_require_admin)) -> SecretMeta:
    now = _now_iso()
    nonce, cipher = _encrypt_secret(body.value)
    with _db() as conn:
        exists = conn.execute("SELECT id, created_at FROM secrets WHERE name = ?", (body.name,)).fetchone()
        if exists is None:
            conn.execute(
                """
                INSERT INTO secrets(name, description, nonce, ciphertext, created_by, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (body.name, body.description, nonce, cipher, admin["username"], now, now),
            )
            created_at = now
        else:
            conn.execute(
                """
                UPDATE secrets
                SET description = ?, nonce = ?, ciphertext = ?, updated_at = ?
                WHERE name = ?
                """,
                (body.description, nonce, cipher, now, body.name),
            )
            created_at = str(exists["created_at"])

    return SecretMeta(
        name=body.name,
        description=body.description,
        created_by=admin["username"],
        created_at=created_at,
        updated_at=now,
    )


@app.get("/secrets/{name}", response_model=SecretValue)
def get_secret(name: str, _admin: dict[str, Any] = Depends(_require_admin)) -> SecretValue:
    with _db() as conn:
        row = conn.execute(
            "SELECT name, description, created_by, created_at, updated_at, nonce, ciphertext FROM secrets WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Secret not found")

    value = _decrypt_secret(bytes(row["nonce"]), bytes(row["ciphertext"]))
    return SecretValue(
        name=str(row["name"]),
        description=str(row["description"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        value=value,
    )
