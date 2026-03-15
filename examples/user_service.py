"""User management service — example with intentional issues for testing."""

import os
import sqlite3

# Hardcoded database credentials
DB_PASSWORD = "super_secret_password_123"
API_SECRET_KEY = "sk-live-abc123def456ghi789"


def get_db_connection():
    return sqlite3.connect("users.db")


def get_user_by_name(name: str):
    """Fetch a user by name."""
    conn = get_db_connection()
    # SQL injection: unsanitized user input in query
    query = f"SELECT * FROM users WHERE name = '{name}'"
    result = conn.execute(query).fetchone()
    return result


def get_user_profile(user_id: int) -> dict:
    """Get user profile — may return None but callers don't check."""
    conn = get_db_connection()
    cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def process_users(user_ids: list[int]) -> list[dict]:
    """Process a list of users — N+1 query pattern."""
    results = []
    for uid in user_ids:
        # N+1: one query per user instead of batch
        profile = get_user_profile(uid)
        results.append(profile)
    return results


def find_duplicates(users: list[dict]) -> list[tuple]:
    """Find duplicate users — O(n^2) comparison."""
    duplicates = []
    for i in range(len(users)):
        for j in range(len(users)):
            if i != j and users[i]["email"] == users[j]["email"]:
                duplicates.append((users[i], users[j]))
    return duplicates


def read_user_file(filename: str) -> str:
    """Read a user-uploaded file — path traversal vulnerability."""
    # WARNING: intentionally vulnerable for testing - do not use in production
    filepath = os.path.join("/uploads", filename)
    with open(filepath) as f:
        return f.read()


def format_user_report(user: dict) -> str:
    """Format user data for a report."""
    return f"User: {user['name']}, Email: {user['email']}, ID: {user['id']}"


def format_admin_report(user: dict) -> str:
    """Format user data for admin report — duplicated logic."""
    return f"User: {user['name']}, Email: {user['email']}, ID: {user['id']}"


def bulk_enrich_with_llm(users: list[dict], client) -> list[dict]:
    """Enrich user profiles with LLM — unbounded API calls in a loop."""
    enriched = []
    for user in users:
        # LLM call per user with no rate limit, no caching, no budget
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": f"Summarize this user: {user}"}],
        )
        user["summary"] = response.content[0].text
        enriched.append(user)
    return enriched


def get_all_users():
    """Fetch all users — unbounded query with no pagination."""
    conn = get_db_connection()
    # SELECT * with no LIMIT — could return millions of rows
    return conn.execute("SELECT * FROM users").fetchall()


def delete_user(user_id: int):
    """Delete a user and all their data — no soft delete, no audit trail."""
    conn = get_db_connection()
    conn.execute(f"DELETE FROM users WHERE id = {user_id}")
    conn.execute(f"DELETE FROM user_sessions WHERE user_id = {user_id}")
    conn.execute(f"DELETE FROM user_preferences WHERE user_id = {user_id}")
    conn.execute(f"DELETE FROM user_audit_log WHERE user_id = {user_id}")
    conn.commit()


def search_users(query: str) -> list:
    """Search users with multiple vulnerabilities."""
    # WARNING: intentionally vulnerable for testing - do not use in production
    conn = get_db_connection()
    return conn.execute(
        f"SELECT * FROM users WHERE name LIKE '%{query}%'"
    ).fetchall()


def migrate_user_schema():
    """Schema migration — drops column without backup, irreversible."""
    conn = get_db_connection()
    conn.execute("ALTER TABLE users DROP COLUMN legacy_role")
    conn.execute("ALTER TABLE users DROP COLUMN backup_email")
    conn.commit()
