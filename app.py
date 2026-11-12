import os
import sqlite3
import secrets
import json
import io
import csv
import random

from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, request, session, redirect,
    render_template, abort, Response, jsonify
)

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "CHANGE_ME_LONG_RANDOM_SECRET"
)

DB = os.environ.get("DATABASE_PATH", "responses.sqlite3")
CORP = "fujisilvertech.com"

OWNER = os.environ.get(
    "INITIAL_ADMIN_EMAIL",
    "Akshay.Korukonda@smartims.com"
).lower()

ADMINS = {OWNER} | {
    x.strip().lower()
    for x in os.environ.get("ADMIN_EMAILS", "").split(",")
    if x.strip()
}

ADMIN_ACCESS_KEY = os.environ.get("ADMIN_ACCESS_KEY", "")

with open("questions.json", encoding="utf-8") as f:
    Q = json.load(f)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


# Create the table and safely add the snapshot column to existing databases.
with conn() as c:
    c.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY,
            email TEXT,
            started TEXT,
            completed TEXT,
            duration INTEGER,
            score INTEGER,
            answers TEXT
        )
    """)

    columns = {
        row["name"]
        for row in c.execute("PRAGMA table_info(attempts)").fetchall()
    }

    if "questions_snapshot" not in columns:
        c.execute("ALTER TABLE attempts ADD COLUMN questions_snapshot TEXT")


def corp(email):
    return (
        email.count("@") == 1
        and email.rsplit("@", 1)[1].lower() == CORP
    )


def signed(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("email"):
            return redirect("/")
        return fn(*args, **kwargs)
    return wrapper


def admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if (
            session.get("email", "").lower() not in ADMINS
            or not session.get("is_admin")
        ):
            abort(403)
