
import os
import sqlite3
import secrets
import json
import io
import csv
import random
import time

from datetime import datetime, timezone
from functools import wraps
from flask import (
    Flask,
    request,
    session,
    redirect,
    render_template,
    abort,
    Response,
    jsonify,
    url_for
)

# =========================================================
# 1. FLASK INITIALIZATION
# IMPORTANT: This must come before every @app.route
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-before-deployment"
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("RENDER", "") == "true"
)

# =========================================================
# 2. CONFIGURATION
# =========================================================

DB = os.environ.get("DATABASE_PATH", "responses.sqlite3")

CORP = "fujisilvertech.com"

OWNER = os.environ.get(
    "INITIAL_ADMIN_EMAIL",
    "Akshay.Korukonda@smartims.com"
).strip().lower()

ADMINS = {OWNER}

ADMINS.update(
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", "").split(",")
    if email.strip()
)

ADMIN_ACCESS_KEY = os.environ.get("ADMIN_ACCESS_KEY", "")

QUESTION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "questions.json"
)

# =========================================================
# 3. LOAD QUESTIONS
# =========================================================

def load_questions():

    if not os.path.exists(QUESTION_FILE):
        raise FileNotFoundError(
            "questions.json was not found. "
            "Make sure it is in the same folder as app.py."
        )

    with open(QUESTION_FILE, "r", encoding="utf-8") as file:
        questions = json.load(file)

    if not isinstance(questions, list) or not questions:
        raise ValueError("questions.json must contain a non-empty list.")

    return questions


Q = load_questions()

# =========================================================
# 4. DATABASE
# =========================================================

def get_db():

    connection = sqlite3.connect(DB, timeout=30)

    connection.row_factory = sqlite3.Row

    return connection


def init_db():

    connection = get_db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            started TEXT,
            completed TEXT,
            duration INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            answers TEXT,
            questions_snapshot TEXT
        )
    """)

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(attempts)"
        ).fetchall()
    }

    if "questions_snapshot" not in columns:
        connection.execute(
            "ALTER TABLE attempts ADD COLUMN questions_snapshot TEXT"
        )

    if "answers" not in columns:
        connection.execute(
            "ALTER TABLE attempts ADD COLUMN answers TEXT"
        )

    connection.commit()
    connection.close()


init_db()

# =========================================================
# 5. GENERAL HELPERS
# =========================================================

def current_time():

    return datetime.now(timezone.utc).isoformat()


def corp(email):

    return email.lower().endswith("@" + CORP)


def signed():

    return bool(session.get("email"))


def admin_required():

    return (
        session.get("email", "").lower() in ADMINS
        and session.get("is_admin") is True
    )


def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not signed():
            return redirect(url_for("home"))

        return function(*args, **kwargs)

    return wrapper


def admin_only(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not signed():
            return redirect(url_for("home"))

        if not admin_required():
            abort(403)

        return function(*args, **kwargs)

    return wrapper


def safe_json(value, fallback):

    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


# =========================================================
# 6. QUESTION HELPERS
# =========================================================

def is_multi_select_question(question):

    return question.get("mode") in (
        "find_mistakes",
        "click_errors"
    )


def get_candidate_ids(question):

    candidates = []

    for item in question.get("mistakes", []):
        if isinstance(item, dict):
            candidates.append(item.get("id"))

    for item in question.get("clickable_items", []):
        if isinstance(item, dict):
            candidates.append(item.get("id"))

    for item in question.get("items", []):
        if isinstance(item, dict):
            candidates.append(item.get("id"))

    return [item for item in candidates if item is not None]


def get_correct_ids(question):

    correct = question.get("correct_ids")

    if correct is None:
        correct = question.get("correct", [])

    if isinstance(correct, str):
        correct = [correct]

    return set(correct or [])


def make_attempt_questions():

    questions = json.loads(json.dumps(Q))

    for question in questions:

        if is_multi_select_question(question):
            continue

        options = question.get("options")

        if not isinstance(options, list) or not options:
            continue

        original_correct = question.get("correct")

        if not isinstance(original_correct, int):
            continue

        if original_correct < 0 or original_correct >= len(options):
            continue

        correct_option = options[original_correct]

        shuffled_options = list(options)
        random.shuffle(shuffled_options)

        question["options"] = shuffled_options
        question["correct"] = shuffled_options.index(correct_option)

    return questions


# =========================================================
# 7. ANSWER VALIDATION AND SCORING
# =========================================================

def validate_and_score(questions, answers):

    if not isinstance(answers, list):
        raise ValueError("Answers must be a list.")

    if len(answers) != len(questions):
        raise ValueError("The number of answers does not match the quiz.")

    score = 0
    reviewed_answers = []

    for index, question in enumerate(questions):

        answer = answers[index]

        if is_multi_select_question(question):

            if answer is None:
                answer = []

            if not isinstance(answer, list):
                raise ValueError(
                    f"Invalid answer format for question {index + 1}."
                )

            correct_ids = get_correct_ids(question)

            selected_ids = set(answer)

            is_correct = selected_ids == correct_ids

            reviewed_answers.append(
                list(selected_ids)
            )

        else:

            if isinstance(answer, bool) or not isinstance(answer, int):
                raise ValueError(
                    f"Invalid answer for question {index + 1}."
                )

            options = question.get("options", [])

            if answer < 0 or answer >= len(options):
                raise ValueError(
                    f"Answer out of range for question {index + 1}."
                )

            correct = question.get("correct")

            is_correct = answer == correct

            reviewed_answers.append(answer)

        if is_correct:
            score += 1

    percentage = round(
        (score / len(questions)) * 100
    ) if questions else 0

    return percentage, reviewed_answers


# =========================================================
# 8. HOME / LOGIN
# =========================================================

@app.route("/", methods=["GET", "POST"])
def home():

    error = None

    if request.method == "POST":

        email = request.form.get(
            "email", ""
        ).strip().lower()

        password = request.form.get(
            "admin_key", ""
        )

        if not email:
            error = "Please enter your email address."

        elif email in ADMINS:

            if not ADMIN_ACCESS_KEY:
                error = (
                    "Admin access is not configured. "
                    "Please configure ADMIN_ACCESS_KEY in Render."
                )

            elif secrets.compare_digest(
                password,
                ADMIN_ACCESS_KEY
            ):

                session.clear()

                session["email"] = email
                session["is_admin"] = True

                return redirect(url_for("admin"))

            else:
                error = "Invalid administrator access key."

        elif corp(email):

            session.clear()

            session["email"] = email
            session["is_admin"] = False

            return redirect(url_for("quiz"))

        else:

            error = (
                "Access restricted. "
                "Please use your authorized corporate email."
            )

    return render_template(
        "home.html",
        error=error
    )


# =========================================================
# 9. QUIZ PAGE
# =========================================================

@app.route("/quiz")
@login_required
def quiz():

    if admin_required():
        return redirect(url_for("admin"))

    attempt_id = secrets.token_urlsafe(16)

    questions = make_attempt_questions()

    started = current_time()

    connection = get_db()

    connection.execute("""
        INSERT INTO attempts (
            id,
            email,
            started,
            completed,
            duration,
            score,
            answers,
            questions_snapshot
        )
        VALUES (?, ?, ?, NULL, 0, 0, NULL, ?)
    """, (
        attempt_id,
        session["email"],
        started,
        json.dumps(questions)
    ))

    connection.commit()
    connection.close()

    session["attempt_id"] = attempt_id

    return render_template(
        "quiz.html",
        questions=questions,
        attempt_id=attempt_id
    )


# =========================================================
# 10. SUBMIT QUIZ
# =========================================================

@app.route("/submit", methods=["POST"])
@login_required
def submit():

    if admin_required():
        return jsonify({
            "success": False,
            "error": "Administrators cannot submit employee quizzes."
        }), 403

    data = request.get_json(silent=True) or {}

    answers = data.get("answers")

    attempt_id = session.get("attempt_id")

    if not attempt_id:
        attempt_id = data.get("attempt_id")

    if not attempt_id:
        return jsonify({
            "success": False,
            "error": "Quiz session expired. Please log in again."
        }), 401

    connection = get_db()

    attempt = connection.execute("""
        SELECT *
        FROM attempts
        WHERE id = ?
        AND email = ?
    """, (
        attempt_id,
        session["email"]
    )).fetchone()

    if not attempt:
        connection.close()

        return jsonify({
            "success": False,
            "error": "Quiz attempt not found."
        }), 404

    if attempt["completed"]:
        connection.close()

        return jsonify({
            "success": True,
            "url": url_for("result", attempt_id=attempt_id)
        })

    # Support either a list or a dictionary from quiz JavaScript.

    if isinstance(answers, dict):

        try:
            answers = [
                answers[str(index)]
                if str(index) in answers
                else answers[index]
                for index in range(len(
                    safe_json(attempt["questions_snapshot"], [])
                ))
            ]

        except (KeyError, TypeError):
            connection.close()

            return jsonify({
                "success": False,
                "error": "Some quiz answers are missing."
            }), 400

    questions = safe_json(
        attempt["questions_snapshot"],
        []
    )

    if not questions:
        connection.close()

        return jsonify({
            "success": False,
            "error": "Question data is missing."
        }), 500

    try:

        percentage, reviewed_answers = validate_and_score(
            questions,
            answers
        )

    except ValueError as error:

        connection.close()

        return jsonify({
            "success": False,
            "error": str(error)
        }), 400

    started = attempt["started"]

    try:
        start_datetime = datetime.fromisoformat(started)
        duration = max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - start_datetime
                ).total_seconds()
            )
        )

    except (ValueError, TypeError):
        duration = 0

    completed = current_time()

    connection.execute("""
        UPDATE attempts
        SET completed = ?,
            duration = ?,
            score = ?,
            answers = ?
        WHERE id = ?
    """, (
        completed,
        duration,
        percentage,
        json.dumps(reviewed_answers),
        attempt_id
    ))

    connection.commit()
    connection.close()

    session.pop("attempt_id", None)

    return jsonify({
        "success": True,
        "url": url_for(
            "result",
            attempt_id=attempt_id
        )
    })


# =========================================================
# 11. RESULT PAGE
# =========================================================

@app.route("/result/<attempt_id>")
@login_required
def result(attempt_id):

    connection = get_db()

    attempt = connection.execute("""
        SELECT *
        FROM attempts
        WHERE id = ?
        AND email = ?
        AND completed IS NOT NULL
    """, (
        attempt_id,
        session["email"]
    )).fetchone()

    connection.close()

    if not attempt:
        abort(404)

    questions = safe_json(
        attempt["questions_snapshot"],
        []
    )

    answers = safe_json(
        attempt["answers"],
        []
    )

    return render_template(
        "result.html",
        r=attempt,
        q=questions,
        a=answers
    )


# =========================================================
# 12. ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
@admin_only
def admin():

    connection = get_db()

    attempts = connection.execute("""
        SELECT *
        FROM attempts
        WHERE completed IS NOT NULL
        ORDER BY completed DESC
    """).fetchall()

    connection.close()

    rows = []

    total_score = 0

    for attempt in attempts:

        questions = safe_json(
            attempt["questions_snapshot"],
            []
        )

        answers = safe_json(
            attempt["answers"],
            []
        )

        total_score += attempt["score"] or 0

        rows.append({
            "id": attempt["id"],
            "email": attempt["email"],
            "started": attempt["started"],
            "completed": attempt["completed"],
            "duration": attempt["duration"],
            "score": attempt["score"],
            "answers": answers,
            "questions": questions
        })

    avg = round(
        total_score / len(rows),
        1
    ) if rows else 0

    return render_template(
        "admin.html",
        rows=rows,
        avg=avg,
        questions=Q
    )


# =========================================================
# 13. CSV EXPORT
# =========================================================

@app.route("/admin/export")
@admin_only
def export_csv():

    connection = get_db()

    attempts = connection.execute("""
        SELECT *
        FROM attempts
        WHERE completed IS NOT NULL
        ORDER BY completed DESC
    """).fetchall()

    connection.close()

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow([
        "Employee Email",
        "Started",
        "Completed",
        "Duration (Seconds)",
        "Score (%)",
        "Answers"
    ])

    for attempt in attempts:

        answers = safe_json(
            attempt["answers"],
            []
        )

        writer.writerow([
            attempt["email"],
            attempt["started"],
            attempt["completed"],
            attempt["duration"],
            attempt["score"],
            json.dumps(answers)
        ])

    csv_data = output.getvalue()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=phishing_assessment_report.csv"
        }
    )


# =========================================================
# 14. LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# =========================================================
# 15. HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "application": "Fuji Silvertech Phishing Awareness"
    })


# =========================================================
# 16. LOCAL DEVELOPMENT
# Render uses gunicorn app:app
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
