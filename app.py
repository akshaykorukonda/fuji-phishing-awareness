        return fn(*args, **kwargs)

    return wrapper


def make_attempt_questions():
    attempt_questions = json.loads(json.dumps(Q))

    positions = [0, 1, 2, 3] * (len(attempt_questions) // 4)
    positions += list(range(len(positions), len(attempt_questions)))
    positions = [p % 4 for p in positions]
    random.shuffle(positions)

    for question, new_position in zip(attempt_questions, positions):
        old_position = question["correct"]
        options = list(question["options"])

        correct_option = options.pop(old_position)
        random.shuffle(options)
        options.insert(new_position, correct_option)

        question["options"] = options
        question["correct"] = new_position

    return attempt_questions


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

    attempt_questions = make_attempt_questions()

    with conn() as c:
        cursor = c.execute(
            """
            INSERT INTO attempts(email, started, questions_snapshot)
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
        email=session["email"]
    )


@app.route("/submit", methods=["POST"])
def submit():
    email = session.get("email")

    if not email or not corp(email):
        return jsonify(error="Please sign in again."), 401

    data = request.get_json(silent=True) or {}
    answers = data.get("answers", [])
    attempt_id = session.get("attempt")

    if not attempt_id:
        return jsonify(error="Assessment attempt not found."), 403

    with conn() as c:
        row = c.execute(
            """
            SELECT started, questions_snapshot
            FROM attempts
            WHERE id=? AND email=? AND completed IS NULL
            """,
            (attempt_id, email)
        ).fetchone()

        if not row:
            return jsonify(error="Assessment attempt not found."), 403

        attempt_questions = json.loads(row["questions_snapshot"])

        if (
            not isinstance(answers, list)
            or len(answers) != len(attempt_questions)
            or any(type(x) is not int or x not in range(4) for x in answers)
        ):
            return jsonify(error="Please answer all questions."), 400

        score = sum(
            answers[i] == attempt_questions[i]["correct"]
            for i in range(len(attempt_questions))
        )

        now = datetime.now(timezone.utc)
        duration = int(
            (now - datetime.fromisoformat(row["started"])).total_seconds()
        )

        c.execute(
            """
            UPDATE attempts
            SET completed=?, duration=?, score=?, answers=?
            WHERE id=?
            """,
            (
                now.isoformat(),
                duration,
                score * 100 // len(attempt_questions),
                json.dumps(answers),
                attempt_id
            )
        )

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

    attempt_questions = json.loads(
        row["questions_snapshot"] or "[]"
    ) or Q

    return render_template(
        "result.html",
        r=row,
        q=attempt_questions,
        a=json.loads(row["answers"] or "[]")
    )


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
        item["answer_list"] = json.loads(item["answers"] or "[]")
        item["question_list"] = json.loads(
            item.get("questions_snapshot") or "[]"
        ) or Q
        rows.append(item)

    average = (
        round(sum(row["score"] for row in rows) / len(rows), 1)
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

    writer.writerow([
        "Email", "Attempt", "Started", "Completed",
        "Duration", "Score"
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
            row["score"]
        ])

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
