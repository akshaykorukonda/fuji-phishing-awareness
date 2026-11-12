
import os
import sqlite3
import secrets
import json
import io
import csv

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

        return fn(*args, **kwargs)

    return wrapper


@app.route("/", methods=["GET", "POST"])
def home():
    msg = ""

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        if not email or email.count("@") != 1:
            msg = "Please enter a valid email address."

        elif email in ADMINS:

            admin_key = request.form.get("admin_key", "")

            if not ADMIN_ACCESS_KEY or not secrets.compare_digest(
                admin_key, ADMIN_ACCESS_KEY
            ):
                msg = "Invalid administrator access key."

            else:
                session.clear()
                session["email"] = email
                session["is_admin"] = True
                return redirect("/admin")

        elif corp(email):

            session.clear()
            session["email"] = email
            session["is_admin"] = False
            return redirect("/quiz")

        else:
            msg = "Only @fujisilvertech.com employee emails are accepted."

    return render_template("home.html", msg=msg)


@app.route("/quiz")
@signed
def quiz():

    if not corp(session["email"]):
        abort(403)

    with conn() as c:

        c.execute(
            "INSERT INTO attempts(email, started) VALUES (?, ?)",
            (
                session["email"],
                datetime.now(timezone.utc).isoformat()
            )
        )

        session["attempt"] = c.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    return render_template(
        "quiz.html",
        questions=Q,
        email=session["email"]
    )


# Submission route: always returns JSON errors instead of HTML redirects
@app.route("/submit", methods=["POST"])
def submit():

    email = session.get("email")

    if not email:
        return jsonify(error="Your session has expired. Please sign in again."), 401

    if not corp(email):
        return jsonify(error="You are not authorized to submit this assessment."), 403

    data = request.get_json(silent=True) or {}
    answers = data.get("answers", [])

    if (
        not isinstance(answers, list)
        or len(answers) != len(Q)
        or any(
            type(x) is not int or x not in range(4)
            for x in answers
        )
    ):
        return jsonify(error="Please answer all questions correctly."), 400

    attempt_id = session.get("attempt")

    if not attempt_id:
        return jsonify(error="Assessment attempt not found. Please restart the quiz."), 403

    score = sum(
        answers[i] == Q[i]["correct"]
        for i in range(len(Q))
    )

    with conn() as c:

        row = c.execute(
            """
            SELECT started
            FROM attempts
            WHERE id=? AND email=? AND completed IS NULL
            """,
            (attempt_id, email)
        ).fetchone()

        if not row:
            return jsonify(
                error="Your assessment attempt could not be found. Please restart the quiz."
            ), 403

        now = datetime.now(timezone.utc)

        duration = int(
            (
                now - datetime.fromisoformat(row["started"])
            ).total_seconds()
        )

        c.execute("""
            UPDATE attempts
            SET completed=?, duration=?, score=?, answers=?
            WHERE id=?
        """, (
            now.isoformat(),
            duration,
            score * 100 // len(Q),
            json.dumps(answers),
            attempt_id
        ))

    return jsonify(
        success=True,
        url="/result/" + str(attempt_id)
    )


@app.route("/result/<int:attempt_id>")
@signed
def result(attempt_id):

    with conn() as c:

        row = c.execute(
            """
            SELECT *
            FROM attempts
            WHERE id=? AND email=? AND completed IS NOT NULL
            """,
            (attempt_id, session["email"])
        ).fetchone()

    if not row:
        abort(404)

    return render_template(
        "result.html",
        r=row,
        q=Q,
        a=json.loads(row["answers"])
    )


# Admin dashboard: includes every employee's individual answers
@app.route("/admin")
@admin
def dashboard():

    with conn() as c:

        db_rows = c.execute("""
            SELECT *
            FROM attempts
            WHERE completed IS NOT NULL
            ORDER BY completed DESC
        """).fetchall()

    rows = []

    for row in db_rows:

        item = dict(row)

        try:
            item["answer_list"] = json.loads(item["answers"] or "[]")
        except (TypeError, json.JSONDecodeError):
            item["answer_list"] = []

        rows.append(item)

    average = (
        round(
            sum(row["score"] for row in rows) / len(rows),
            1
        )
        if rows else 0
    )

    return render_template(
        "admin.html",
        rows=rows,
        avg=average,
        questions=Q
    )


@app.route("/admin/export")
@admin
def export():

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "Email",
            "Attempt",
            "Started",
            "Completed",
            "Duration",
            "Score"
        ] +
        ["Q" + str(i) for i in range(1, len(Q) + 1)]
    )

    with conn() as c:

        rows = c.execute("""
            SELECT *
            FROM attempts
            WHERE completed IS NOT NULL
            ORDER BY completed DESC
        """).fetchall()

    for row in rows:

        answers = json.loads(row["answers"] or "[]")

        writer.writerow([
            row["email"],
            row["id"],
            row["started"],
            row["completed"],
            row["duration"],
            row["score"]
        ] + answers)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            "attachment; filename=fuji-awareness.csv"
        }
    )


@app.route("/logout")
def logout():

    session.clear()
    return redirect("/")


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
