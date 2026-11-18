
@app.route("/submit", methods=["POST"])
def submit():

    email = session.get("email")

    if not email:
        return jsonify(
            error="Your session has expired. Please sign in again."
        ), 401

    if not corp(email):
        return jsonify(error="You are not authorized."), 403

    data = request.get_json(silent=True) or {}
    answers = data.get("answers", [])
    attempt_id = session.get("attempt")

    if not attempt_id:
        return jsonify(error="Assessment attempt not found."), 403

    # Support both list and object formats from the quiz JavaScript.
    if isinstance(answers, dict):
        try:
            answers = [
                answers[str(i)] if str(i) in answers else answers[i]
                for i in range(len(Q))
            ]
        except (KeyError, TypeError):
            return jsonify(error="Some answers are missing."), 400

    if not isinstance(answers, list):
        return jsonify(error="Invalid answer format."), 400

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

        attempt_questions = json.loads(
            row["questions_snapshot"] or "[]"
        )

        if not attempt_questions:
            return jsonify(error="Question data not found."), 500

        try:
            validated_answers, score = validate_and_score(
                answers,
                attempt_questions
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400

        now = datetime.now(timezone.utc)

        duration = int(
            (
                now - datetime.fromisoformat(row["started"])
            ).total_seconds()
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
                score,
                json.dumps(validated_answers),
                attempt_id
            )
        )

    return jsonify(
        success=True,
        url="/result/" + str(attempt_id)
    )
