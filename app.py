
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
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    app.secret_key = "development-only-change-this-secret-key"

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("RENDER", "").lower() == "true"
)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# =========================================================
# 2. CONFIGURATION
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB = os.environ.get(
    "DATABASE_PATH",
    os.path.join(BASE_DIR, "responses.sqlite3")
)

CORP = "fujisilvertech.com"

PASS_MARK = 80

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
    BASE_DIR,
    "questions.json"
)

REQUIRED_SETS = ("A", "B", "C")

# =========================================================
# 3. LOAD AND VALIDATE QUESTION SETS
# =========================================================

def load_questions():

    if not os.path.exists(QUESTION_FILE):
        raise FileNotFoundError(
            "questions.json was not found. "
            "Place it in the same folder as app.py."
        )

    with open(
        QUESTION_FILE,
        "r",
        encoding="utf-8"
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict) or "sets" not in data:
        raise ValueError(
            "questions.json must contain a 'sets' object."
        )

    sets = data["sets"]

    if not isinstance(sets, dict):
        raise ValueError("'sets' must be a JSON object.")

    for set_name in REQUIRED_SETS:

        questions = sets.get(set_name)

        if not isinstance(questions, list) or len(questions) != 10:
            raise ValueError(
                f"Set {set_name} must contain exactly 10 questions."
            )

        for index, question in enumerate(questions):

            if not isinstance(question, dict):
                raise ValueError(
                    f"Set {set_name}, question {index + 1} is invalid."
                )

            options = question.get("options")
            correct = question.get("correct")

            if not isinstance(options, list) or len(options) < 2:
                raise ValueError(
                    f"Set {set_name}, question {index + 1} "
                    "must contain at least two options."
                )

            if (
                isinstance(correct, bool)
                or not isinstance(correct, int)
                or correct < 0
                or correct >= len(options)
            ):
                raise ValueError(
                    f"Invalid correct answer in Set {set_name}, "
                    f"question {index + 1}."
                )

            if not all(isinstance(option, str) for option in options):
                raise ValueError(
                    f"All options must be text in Set {set_name}, "
                    f"question {index + 1}."
                )

    return {
        name: sets[name]
        for name in REQUIRED_SETS
    }


QUESTION_SETS = load_questions()

# =========================================================
# 4. DATABASE
# =========================================================

def get_db():

    connection = sqlite3.connect(
        DB,
        timeout=30
    )

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
            questions_snapshot TEXT,
            set_number TEXT
        )
    """)

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(attempts)"
        ).fetchall()
    }

    migrations = {
        "questions_snapshot": "TEXT",
        "answers": "TEXT",
        "set_number": "TEXT",
        "duration": "INTEGER DEFAULT 0",
        "score": "INTEGER DEFAULT 0"
    }

    for column, column_type in migrations.items():

        if column not in columns:
            connection.execute(
                f"ALTER TABLE attempts ADD COLUMN {column} {column_type}"
            )

    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_attempts_email
        ON attempts(email)
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_attempts_completed
        ON attempts(completed)
    """)

    connection.commit()
    connection.close()


init_db()

# =========================================================
# 5. GENERAL HELPERS
# =========================================================

def current_time():

    return datetime.now(timezone.utc).isoformat()


def corp(email):

    email = email.strip().lower()

    return (
        "@" in email
        and email.endswith("@" + CORP)
    )


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


def is_passed(score):

    try:
        return float(score or 0) >= PASS_MARK

    except (ValueError, TypeError):
        return False


def get_owned_attempt(attempt_id, email):

    connection = get_db()

    attempt = connection.execute("""
        SELECT *
        FROM attempts
        WHERE id = ?
        AND email = ?
    """, (
        attempt_id,
        email
    )).fetchone()

    connection.close()

    return attempt


# =========================================================
# 6. QUESTION HELPERS
# =========================================================

ANSWER_FIELDS = {
    "correct",
    "correct_ids",
    "correct_mistakes",
    "correct_errors",
    "explanation",
    "answer_key",
    "correct_answer",
    "correct_answers"
}


def sanitize_question(question):
    """
    Remove correct answers and explanations before
    sending questions to the employee's browser.
    """

    if isinstance(question, list):

        return [
            sanitize_question(item)
            for item in question
        ]

    if isinstance(question, dict):

        clean = {}

        for key, value in question.items():

            if key.lower() in ANSWER_FIELDS:
                continue

            clean[key] = sanitize_question(value)

        return clean

    return question


def is_multi_select_question(question):

    return question.get("mode") in (
        "find_mistakes",
        "click_errors"
    )


def get_correct_ids(question):

    correct = question.get("correct_ids")

    if correct is None:
        correct = question.get("correct", [])

    if isinstance(correct, str):
        correct = [correct]

    return {
        str(item)
        for item in (correct or [])
    }


def make_attempt_questions(set_number):

    questions = json.loads(
        json.dumps(QUESTION_SETS[set_number])
    )

    # Keep question and option order unchanged.
    # This preserves the original correct-answer indexes.

    return questions


# =========================================================
# 7. ANSWER VALIDATION AND SCORING
# =========================================================

def validate_and_score(questions, answers):

    if not isinstance(answers, list):
        raise ValueError("Answers must be a list.")

    if len(answers) != len(questions):
        raise ValueError(
            "The number of answers does not match the quiz."
        )

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

            selected_ids = {
                str(item)
                for item in answer
            }

            is_correct = selected_ids == correct_ids

            reviewed_answers.append(
                sorted(selected_ids)
            )

        else:

            options = question.get("options", [])

            if answer is None:

                is_correct = False
                reviewed_answers.append(None)

            else:

                if (
                    isinstance(answer, bool)
                    or not isinstance(answer, int)
                ):
                    raise ValueError(
                        f"Invalid answer for question {index + 1}."
                    )

                if answer < 0 or answer >= len(options):
                    raise ValueError(
                        f"Answer out of range for question {index + 1}."
                    )

                is_correct = (
                    answer == question.get("correct")
                )

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
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "admin_key",
            ""
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

                return redirect(
                    url_for("admin")
                )

            else:

                error = "Invalid administrator access key."

        elif corp(email):

            session.clear()

            session["email"] = email
            session["is_admin"] = False

            return redirect(
                url_for("quiz")
            )

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

    connection = get_db()

    # Resume an existing incomplete attempt.

    attempt = connection.execute("""
        SELECT *
        FROM attempts
        WHERE email = ?
        AND completed IS NULL
        ORDER BY started DESC
        LIMIT 1
    """, (
        session["email"],
    )).fetchone()

    if attempt:

        attempt_id = attempt["id"]

        set_number = (
            attempt["set_number"] or "A"
        )

        questions = safe_json(
            attempt["questions_snapshot"],
            []
        )

        if not questions:

            connection.close()

            return (
                "The saved quiz question data is missing. "
                "Please contact the administrator.",
                500
            )

    else:

        # Assign one random set to a new attempt.

        set_number = random.choice(
            list(REQUIRED_SETS)
        )

        attempt_id = secrets.token_urlsafe(16)

        questions = make_attempt_questions(
            set_number
        )

        started = current_time()

        connection.execute("""
            INSERT INTO attempts (
                id,
                email,
                started,
                completed,
                duration,
                score,
                answers,
                questions_snapshot,
                set_number
            )
            VALUES (?, ?, ?, NULL, 0, 0, NULL, ?, ?)
        """, (
            attempt_id,
            session["email"],
            started,
            json.dumps(questions),
            set_number
        ))

        connection.commit()

    connection.close()

    session["attempt_id"] = attempt_id

    participant_questions = [
        sanitize_question(question)
        for question in questions
    ]

    return render_template(
        "quiz.html",
        questions=participant_questions,
        attempt_id=attempt_id,
        set_number=set_number
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

    # Prevent a second submission from changing the result.

    if attempt["completed"]:

        connection.close()

        return jsonify({
            "success": True,
            "url": url_for(
                "result",
                attempt_id=attempt_id
            )
        })

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

    # Support a list or a dictionary from JavaScript.

    if isinstance(answers, dict):

        try:

            answers = [
                answers.get(
                    str(index),
                    answers.get(index)
                )
                for index in range(len(questions))
            ]

        except (KeyError, TypeError):

            connection.close()

            return jsonify({
                "success": False,
                "error": "Some quiz answers are missing."
            }), 400

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

        start_datetime = datetime.fromisoformat(
            started
        )

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

    # Conditional update prevents duplicate submissions.

    cursor = connection.execute("""
        UPDATE attempts
        SET completed = ?,
            duration = ?,
            score = ?,
            answers = ?
        WHERE id = ?
        AND email = ?
        AND completed IS NULL
    """, (
        completed,
        duration,
        percentage,
        json.dumps(reviewed_answers),
        attempt_id,
        session["email"]
    ))

    connection.commit()

    if cursor.rowcount == 0:

        connection.close()

        return jsonify({
            "success": False,
            "error": "This assessment has already been submitted."
        }), 409

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

    return render_template(
        "result.html",
        r=attempt,
        passed=is_passed(attempt["score"]),
        pass_mark=PASS_MARK
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

        score = attempt["score"] or 0

        total_score += score

        set_number = (
            attempt["set_number"] or "Legacy"
        )

        rows.append({
            "id": attempt["id"],
            "email": attempt["email"],
            "started": attempt["started"],
            "completed": attempt["completed"],
            "duration": attempt["duration"],
            "score": score,
            "set_number": set_number,
            "passed": is_passed(score),
            "status": (
                "PASSED"
                if is_passed(score)
                else "NOT PASSED"
            ),

            "answers": answers,
            "questions": questions,

            # Template compatibility aliases.

            "question_list": questions,
            "answer_list": answers
        })

    avg = round(
        total_score / len(rows),
        1
    ) if rows else 0

    return render_template(
        "admin.html",
        rows=rows,
        avg=avg,
        questions=QUESTION_SETS,
        pass_mark=PASS_MARK
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
        "Assigned Set",
        "Started",
        "Completed",
        "Duration (Seconds)",
        "Score (%)",
        "Pass/Fail",
        "Answers"
    ])

    for attempt in attempts:

        answers = safe_json(
            attempt["answers"],
            []
        )

        score = attempt["score"] or 0

        status = (
            "PASSED"
            if is_passed(score)
            else "NOT PASSED"
        )

        writer.writerow([
            attempt["email"],
            attempt["set_number"] or "Legacy",
            attempt["started"],
            attempt["completed"],
            attempt["duration"],
            score,
            status,
            json.dumps(answers)
        ])

    csv_data = "\ufeff" + output.getvalue()

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

    return redirect(
        url_for("home")
    )


# =========================================================
# 15. HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "application": "Fuji Silvertech Phishing Awareness",
        "pass_mark": PASS_MARK,
        "question_sets": list(REQUIRED_SETS)
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
