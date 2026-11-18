
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

# Load question bank
with open("questions.json", encoding="utf-8") as f:
    Q = json.load(f)


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

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

    columns = {
        row["name"]
        for row in c.execute(
            "PRAGMA table_info(attempts)"
        ).fetchall()
    }

    if "questions_snapshot" not in columns:
        c.execute(
            "ALTER TABLE attempts ADD COLUMN questions_snapshot TEXT"
        )


# --------------------------------------------------
# QUESTION HELPERS
# --------------------------------------------------

def is_multi_select_question(question):
    return question.get("mode") in (
        "find_mistakes",
        "click_errors"
    )


def get_candidate_ids(question):
    """
    Return every selectable ID for a multi-select question.
    Includes decoy options so incorrect selections can be scored.
    """

    mode = question.get("mode")
    candidate_ids = set()

    if mode == "find_mistakes":

        for item in question.get("mistakes", []):
            if isinstance(item, dict) and item.get("id"):
                candidate_ids.add(str(item["id"]))

    elif mode == "click_errors":

        # Clickable sender-address parts
        for item in question.get("sender_parts", []):
            if (
                isinstance(item, dict)
                and item.get("clickable")
                and item.get("id")
            ):
                candidate_ids.add(str(item["id"]))

        # Clickable email-body parts
        for item in question.get("email_parts", []):
            if (
                isinstance(item, dict)
                and item.get("clickable")
                and item.get("id")
            ):
                candidate_ids.add(str(item["id"]))

    return candidate_ids


def get_correct_ids(question):
    """
    Return the actual correct IDs for multi-select questions.
    """

    mode = question.get("mode")

    if mode == "find_mistakes":

        correct = question.get("correct_mistakes")

        if correct is not None:
            return {str(item) for item in correct}

        # Backward compatibility
        return {
            str(item["id"])
            for item in question.get("mistakes", [])
            if isinstance(item, dict) and item.get("id")
        }

    if mode == "click_errors":

        return {
            str(item)
            for item in question.get("correct_errors", [])
        }

    return set()


def make_attempt_questions():
    """
    Copy the question bank and randomize only
    multiple-choice answer positions.

    Multi-select questions retain their original IDs.
    """

    attempt_questions = json.loads(json.dumps(Q))

    mc_questions = [
        question
        for question in attempt_questions
        if not is_multi_select_question(question)
    ]

    positions = [0, 1, 2, 3] * (
        len(mc_questions) // 4
    )

    positions += list(range(
        len(positions),
        len(mc_questions)
    ))

    positions = [position % 4 for position in positions]

    random.shuffle(positions)

    for question, new_position in zip(
        mc_questions,
        positions
    ):

        options = question.get("options", [])
        old_position = question.get("correct")

        if (
            not isinstance(options, list)
            or not options
            or type(old_position) is not int
            or old_position not in range(len(options))
        ):
            raise ValueError(
                "Invalid multiple-choice question: "
                + question.get("title", "Unknown question")
            )

        correct_option = options[old_position]

        other_options = [
            option
            for index, option in enumerate(options)
            if index != old_position
        ]

        random.shuffle(other_options)

        question["options"] = other_options
        question["options"].insert(
            new_position,
            correct_option
        )

        question["correct"] = new_position

    return attempt_questions


# --------------------------------------------------
# ANSWER VALIDATION AND SCORING
# --------------------------------------------------

def validate_and_score(answers, questions):
    """
    Multiple-choice:
        Answer is an integer option index.

    Find-the-mistakes:
        Answer is a list of selected mistake IDs.

    Click-errors:
        Answer is a list of selected clickable email IDs.

    Incorrect selections reduce the multi-select score.
    """

    if not isinstance(answers, list):
        raise ValueError("Invalid answer format.")

    if len(answers) != len(questions):
        raise ValueError("Please answer every question.")

    validated_answers = []
    question_scores = []

    for index, question in enumerate(questions):

        answer = answers[index]

        # ------------------------------------------
        # MULTI-SELECT QUESTIONS
        # ------------------------------------------

        if is_multi_select_question(question):

            candidate_ids = get_candidate_ids(question)
            correct_ids = get_correct_ids(question)

            if not candidate_ids or not correct_ids:
                raise ValueError(
                    "Invalid multi-select question configuration: "
                    + question.get("title", "Unknown question")
                )

            if not correct_ids.issubset(candidate_ids):
                raise ValueError(
                    "Correct answer IDs are missing from selectable items: "
                    + question.get("title", "Unknown question")
                )

            if (
                not isinstance(answer, list)
                or not answer
                or any(not isinstance(item, str) for item in answer)
                or len(answer) != len(set(answer))
            ):
                raise ValueError(
                    "Please select at least one detail for every investigation question."
                )

            selected_ids = set(answer)

            if not selected_ids.issubset(candidate_ids):
                raise ValueError(
                    "Invalid selection in question: "
                    + question.get("title", "Unknown question")
                )

            correct_selections = len(
                selected_ids.intersection(correct_ids)
            )

            incorrect_selections = len(
                selected_ids - correct_ids
            )

            raw_score = max(
                0,
                correct_selections - incorrect_selections
            )

            question_score = min(
                1,
                raw_score / len(correct_ids)
            )

            validated_answers.append(
                sorted(selected_ids)
            )

            question_scores.append(question_score)

        # ------------------------------------------
        # MULTIPLE-CHOICE QUESTIONS
        # ------------------------------------------

        else:

            options = question.get("options", [])
            correct = question.get("correct")

            if (
                type(answer) is not int
                or not isinstance(options, list)
                or answer not in range(len(options))
                or type(correct) is not int
                or correct not in range(len(options))
            ):
                raise ValueError(
                    "Please select a valid answer for every question."
                )

            validated_answers.append(answer)

            question_scores.append(
                1 if answer == correct else 0
            )

    if not question_scores:
        raise ValueError("No questions were found.")

    percentage = round(
        sum(question_scores) * 100 / len(question_scores)
    )

    return validated_answers, percentage


# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

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


# --------------------------------------------------
# HOME / LOGIN
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def home():

    msg = ""

    if request.method == "POST":

        email = request.form.get(
            "email", ""
        ).strip().lower()

        if not email or email.count("@") != 1:

            msg = "Please enter a valid email address."

        elif email in ADMINS:

            admin_key = request.form.get(
                "admin_key", ""
            )

            if not ADMIN_ACCESS_KEY or not secrets.compare_digest(
                admin_key,
                ADMIN_ACCESS_KEY
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

            msg = (
                "Only @fujisilvertech.com employee emails "
                "are accepted."
            )

    return render_template(
        "home.html",
        msg=msg
    )


# --------------------------------------------------
# START QUIZ
# --------------------------------------------------

@app.route("/quiz")
@signed
def quiz():

    if not corp(session["email"]):
        abort(403)

    attempt_questions = make_attempt_questions()

    with conn() as c:

        cursor = c.execute(
            """
            INSERT INTO attempts(
                email,
                started,
                questions_snapshot
            )
            VALUES (?, ?, ?)
            """,
            (
                session["email"],
                datetime.now(timezone.utc).isoformat(),
                json.dumps(attempt_questions)
            )
        )

        attempt_id = cursor.lastrowid

    session["attempt"] = attempt_id

    return render_template(
        "quiz.html",
        questions=attempt_questions,
        email=session["email"],
        attempt_id=attempt_id
    )


# --------------------------------------------------
# SUBMIT QUIZ
# --------------------------------------------------

@app.route("/submit", methods=["POST"])
def submit():

    email = session.get("email")

    if not email:
        return jsonify(
            error="Your session has expired. Please sign in again."
        ), 401

    if not corp(email):
        return jsonify(
            error="You are not authorized."
        ), 403

    data = request.get_json(silent=True) or {}

    answers = data.get("answers", [])

    attempt_id = session.get("attempt")

    if not attempt_id:
        return jsonify(
            error="Assessment attempt not found."
        ), 403

    with conn() as c:

        row = c.execute(
            """
            SELECT
                started,
                questions_snapshot
            FROM attempts
            WHERE id=?
            AND email=?
            AND completed IS NULL
            """,
            (
                attempt_id,
                email
            )
        ).fetchone()

        if not row:
            return jsonify(
                error="Assessment attempt not found."
            ), 403

        attempt_questions = json.loads(
            row["questions_snapshot"] or "[]"
        )

        if not attempt_questions:
            return jsonify(
                error="Question data not found."
            ), 500

        try:

            validated_answers, score = validate_and_score(
                answers,
                attempt_questions
            )

        except ValueError as error:

            return jsonify(
                error=str(error)
            ), 400

        now = datetime.now(timezone.utc)

        duration = int(
            (
                now - datetime.fromisoformat(row["started"])
            ).total_seconds()
        )

        c.execute(
            """
            UPDATE attempts
            SET
                completed=?,
                duration=?,
                score=?,
                answers=?
            WHERE id=?
            """,
            (
                now.isoformat(),
                duration,
                score,
                json.dumps(validated_answers),
                attempt_id
            )
        )

    return jsonify(
        success=True,
        url="/result/" + str(attempt_id)
    )


# --------------------------------------------------
# RESULT PAGE
# --------------------------------------------------

@app.route("/result/<int:attempt_id>")
@signed
def result(attempt_id):

    with conn() as c:

        row = c.execute(
            """
            SELECT *
            FROM attempts
            WHERE id=?
            AND email=?
            AND completed IS NOT NULL
            """,
            (
                attempt_id,
                session["email"]
            )
        ).fetchone()

    if not row:
        abort(404)

    attempt_questions = json.loads(
        row["questions_snapshot"] or "[]"
    ) or Q

    answers = json.loads(
        row["answers"] or "[]"
    )

    return render_template(
        "result.html",
        r=row,
        q=attempt_questions,
        a=answers
    )


# --------------------------------------------------
# ADMIN DASHBOARD
# --------------------------------------------------

@app.route("/admin")
@admin
def dashboard():

    with conn() as c:

        db_rows = c.execute(
            """
            SELECT *
            FROM attempts
            WHERE completed IS NOT NULL
            ORDER BY completed DESC
            """
        ).fetchall()

    rows = []

    for row in db_rows:

        item = dict(row)

        item["answer_list"] = json.loads(
            item["answers"] or "[]"
        )

        item["question_list"] = json.loads(
            item.get("questions_snapshot") or "[]"
        ) or Q

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


# --------------------------------------------------
# CSV EXPORT
# --------------------------------------------------

@app.route("/admin/export")
@admin
def export():

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Email",
        "Attempt",
        "Started",
        "Completed",
        "Duration (seconds)",
        "Score (%)",
        "Answers"
    ])

    with conn() as c:

        rows = c.execute(
            """
            SELECT *
            FROM attempts
            WHERE completed IS NOT NULL
            ORDER BY completed DESC
            """
        ).fetchall()

    for row in rows:

        writer.writerow([
            row["email"],
            row["id"],
            row["started"],
            row["completed"],
            row["duration"],
            row["score"],
            row["answers"]
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            "attachment; filename=fuji-awareness.csv"
        }
    )


# --------------------------------------------------
# LOGOUT
# --------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# --------------------------------------------------
# RUN APPLICATION
# --------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
