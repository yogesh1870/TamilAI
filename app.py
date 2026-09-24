import difflib
import os
import re
import time
import requests

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# OLLAMA

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "tamil-llama"


def load_system_prompt(modelfile_name="Modelfile"):
    """Extract the text inside SYSTEM \"\"\" ... \"\"\" from the Ollama Modelfile
    that sits next to this app.py."""
    modelfile_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        modelfile_name,
    )

    with open(modelfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r'SYSTEM\s*"""(.*?)"""', content, re.DOTALL)

    if not match:
        raise RuntimeError(
            f"Could not find a SYSTEM \"\"\" ... \"\"\" block in {modelfile_path}. "
            "app.py reads the system prompt from the Modelfile at startup -- "
            "make sure Modelfile is in the same folder as app.py and still "
            "has a SYSTEM block."
        )

    return match.group(1).strip()


SYSTEM_PROMPT = load_system_prompt()

# NOTE: num_predict is now chosen per-request in call_ollama_once() based on
# input length, since a fixed 512 was too small for paragraphs and too slow
# as a default for single sentences. These are just the base/floor values.
#
# temperature/top_p/top_k/seed are pinned to make output deterministic. A
# grammar-correction task should give the same answer for the same input
# every time -- with default sampling, the exact same sentence can come
# back "corrected" on one call and untouched (wrongly, as if it were
# already fine) on the next, purely from random token sampling. Greedy
# decoding (temperature 0) removes that randomness; the fixed seed removes
# any residual variance from Ollama's own RNG initialization.
OLLAMA_BASE_OPTIONS = {
    "num_ctx": 4096,       # was 2048 -- too small once a paragraph + the
                            # instruction wrapper is included in the prompt
    "num_predict": 512,    # floor; bumped up dynamically for longer input
    "temperature": 0,      # greedy decoding -- always pick the most likely
                            # token instead of sampling randomly
    "top_p": 1,
    "top_k": 1,
    "seed": 42,             # fixes any remaining RNG so repeats are stable
}

OLLAMA_KEEP_ALIVE = "30m"
MAX_OLLAMA_ATTEMPTS = 2


# DB

DB_CONFIG = dict(
    host="localhost",
    dbname="tamil",
    user="postgres",
    password="tamil123",
)


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id SERIAL PRIMARY KEY,
            user_id INT REFERENCES users(user_id),
            input_text TEXT,
            clean_text TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS corrections (
            correction_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            error_type VARCHAR(30),
            corrected_text TEXT,
            explanation TEXT
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            score_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            spelling_score NUMERIC(5, 2),
            grammar_score NUMERIC(5, 2),
            sentence_score NUMERIC(5, 2),
            overall_score NUMERIC(5, 2)
        );
        """
    )

    conn.commit()
    cur.close()
    conn.close()


# =========================
# HOME / AUTH / CHAT PAGE (unchanged)
# =========================

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("chat"))
    return render_template("index.html", active_tab="login")


@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not email or not password:
        return render_template(
            "index.html", active_tab="signup",
            signup_error="அனைத்து விவரங்களையும் நிரப்பவும்.",
        )

    if len(password) < 8:
        return render_template(
            "index.html", active_tab="signup",
            signup_error="கடவுச்சொல் குறைந்தது 8 எழுத்துகள் இருக்க வேண்டும்.",
        )

    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT user_id FROM users WHERE username = %s OR email = %s",
            (username, email),
        )
        if cur.fetchone():
            return render_template(
                "index.html", active_tab="signup",
                signup_error="இந்த பயனர் பெயர் அல்லது மின்னஞ்சல் ஏற்கனவே பதிவு செய்யப்பட்டுள்ளது.",
            )

        cur.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s) RETURNING user_id
            """,
            (username, email, password_hash),
        )
        user_id = cur.fetchone()[0]
        conn.commit()

    finally:
        cur.close()
        conn.close()

    session["user_id"] = user_id
    session["username"] = username
    return redirect(url_for("chat"))


@app.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not identifier or not password:
        return render_template(
            "index.html", active_tab="login",
            login_error="பயனர் பெயரும் கடவுச்சொல்லும் தேவை.",
        )

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            "SELECT user_id, username, password_hash FROM users "
            "WHERE username = %s OR email = %s",
            (identifier, identifier),
        )
        user = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return render_template(
            "index.html", active_tab="login",
            login_error="கணக்கு இல்லை அல்லது தவறான கடவுச்சொல். பதிவு செய்யவும்.",
        )

    session["user_id"] = user["user_id"]
    session["username"] = user["username"]
    return redirect(url_for("chat"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/chat")
def chat():
    if not session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("chat.html", username=session.get("username"))


# =========================
# DETERMINISTIC ERROR/SCORE COMPUTATION
# =========================
# The fine-tuned tamil-llama model has proven (via `ollama run`) that it can
# reliably do exactly one thing: turn an incorrect Tamil sentence/paragraph
# into a corrected one, replying as "Corrected Tamil Sentence: ...". It does
# NOT reliably produce the structured multi-section report (error lists,
# per-category scores). So we take the one output it's actually good at
# (the corrected text) and compute everything else -- error lists and
# scores -- ourselves with a plain word-level diff. This is deterministic:
# it always produces real numbers and real error entries on every request.

CORRECTED_LABEL_PATTERNS = [
    r"corrected\s+tamil\s+sentence\s*:\s*(.*)",
    r"திருத்தப்பட்ட\s+(?:தமிழ்\s+)?வாக்கியம்\s*:\s*(.*)",
    r"corrected\s+text\s*:\s*(.*)",
]

# Defensive check for the failure mode we actually saw -- the model echoing
# the literal template placeholders back instead of filling them in.
TEMPLATE_ECHO_MARKERS = ["XX/100", "[complete corrected", "Error 1:\nWrong: ...", "Wrong: ..."]

# A label that would mark the start of a trailing section we don't want
# folded into the corrected text (e.g. the model adding its own notes).
TRAILING_SECTION_RE = re.compile(
    r"\n\s*\n|\n(?=(?:Explanation|Note|Error|Score|விளக்கம்|குறிப்பு|பிழை|மதிப்பெண்)\s*[:：])",
    re.IGNORECASE,
)


def extract_corrected_sentence(reply_text, original_text):
    """Pull the corrected sentence/paragraph out of the model's reply.

    IMPORTANT: this keeps the full multi-line corrected text (previously it
    took only the first line via splitlines()[0], which silently truncated
    any paragraph input down to its first sentence).

    Also guards against a different failure mode seen with longer/complex
    input: the fine-tuned model sometimes ignores the actual input and
    returns a short, unrelated sentence it has apparently memorized from
    training, instead of a real correction. A "correction" that is far
    shorter than the original is treated as a failed parse rather than
    presented as a real result -- see is_suspiciously_short() below."""
    if not reply_text:
        return original_text, False

    for marker in TEMPLATE_ECHO_MARKERS:
        if marker in reply_text:
            # The model echoed our instructions instead of answering.
            return original_text, False

    for pattern in CORRECTED_LABEL_PATTERNS:
        match = re.search(pattern, reply_text, re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            # Cut off a trailing explanation/notes section if the model
            # added one, but keep every line of the actual corrected text.
            candidate = TRAILING_SECTION_RE.split(candidate)[0].strip()
            if candidate and not is_suspiciously_short(original_text, candidate):
                return candidate, True

    # No recognizable label -- if the reply is plausible free text (not an
    # obvious multi-paragraph ramble), use the whole thing as-is.
    cleaned = reply_text.strip()
    if cleaned and len(cleaned) < 1000 and not is_suspiciously_short(original_text, cleaned):
        return cleaned, True

    return original_text, False


def is_suspiciously_short(original_text, candidate_text):
    """A real correction should be roughly the same length as the input --
    grammar/spelling fixes don't delete most of a sentence. If the model's
    reply is much shorter than the input once the input is long enough for
    that to be meaningful, it's more likely the model gave up and returned
    a stock/memorized sentence than a genuine correction of everything the
    user wrote. Short inputs are exempt since normal single-word/short-
    phrase corrections can legitimately shrink a lot (e.g. removing a
    duplicated word)."""
    original_words = len(original_text.split())
    candidate_words = len(candidate_text.split())

    if original_words < 8:
        return False

    return candidate_words < original_words * 0.5


def char_similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def diff_errors(original_text, corrected_text):
    """Word-level diff between the original and corrected sentence/paragraph.
    Returns (spelling_errors, grammar_errors) lists of
    {wrong, correct, explanation} dicts.

    Classification heuristic: for each changed word/phrase, compare the
    character-level similarity of the 'wrong' vs 'correct' form. A small
    edit (most characters shared, e.g. a single letter fixed) is treated as
    a spelling error; a larger change (different word/suffix/tense) is
    treated as a grammar error. This is a simple, explainable heuristic --
    not true grammatical parsing -- but it is deterministic and always
    produces a real, defensible answer.
    """
    orig_words = original_text.split()
    corr_words = corrected_text.split()

    matcher = difflib.SequenceMatcher(None, orig_words, corr_words)

    spelling_errors = []
    grammar_errors = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        wrong_chunk = " ".join(orig_words[i1:i2]) or "(விடுபட்டுள்ளது)"
        correct_chunk = " ".join(corr_words[j1:j2]) or "(நீக்கப்பட்டுள்ளது)"

        similarity = char_similarity(wrong_chunk, correct_chunk)

        if tag == "replace" and similarity >= 0.6:
            spelling_errors.append({
                "wrong": wrong_chunk,
                "correct": correct_chunk,
                "explanation": f"'{wrong_chunk}' என்பது எழுத்துப் பிழையுடன் உள்ளது; சரியான வடிவம் '{correct_chunk}'.",
            })
        else:
            label = "மாற்றப்பட வேண்டும்" if tag == "replace" else (
                "நீக்கப்பட வேண்டும்" if tag == "delete" else "சேர்க்கப்பட வேண்டும்"
            )
            grammar_errors.append({
                "wrong": wrong_chunk,
                "correct": correct_chunk,
                "explanation": f"'{wrong_chunk}' {label}; சரியான வடிவம் '{correct_chunk}' (இலக்கணம்/வாக்கிய அமைப்பு).",
            })

    return spelling_errors, grammar_errors


def compute_result(original_text, model_reply):
    corrected_text, parsed_ok = extract_corrected_sentence(model_reply, original_text)

    total_words = max(len(original_text.split()), 1)
    spelling_errors, grammar_errors = diff_errors(original_text, corrected_text)

    spelling_count = len(spelling_errors)
    grammar_count = len(grammar_errors)

    spelling_score = round(max(0, (total_words - spelling_count) / total_words * 100))
    grammar_score = round(max(0, (total_words - grammar_count) / total_words * 100))
    sentence_score = round(max(0, (total_words - spelling_count - grammar_count) / total_words * 100))
    overall_score = round(0.3 * spelling_score + 0.4 * grammar_score + 0.3 * sentence_score)

    if spelling_count == 0 and grammar_count == 0:
        overall_explanation = "வாக்கியத்தில்/பத்தியில் பிழைகள் எதுவும் இல்லை."
    else:
        overall_explanation = (
            f"மொத்தம் {spelling_count} எழுத்துப் பிழை(கள்) மற்றும் "
            f"{grammar_count} இலக்கணப் பிழை(கள்) கண்டறியப்பட்டு திருத்தப்பட்டுள்ளன."
        )

    return {
        "parsed_ok": parsed_ok,
        "corrected_text": corrected_text,
        "spelling_errors": spelling_errors,
        "spelling_score": spelling_score,
        "grammar_errors": grammar_errors,
        "grammar_score": grammar_score,
        "sentence_score": sentence_score,
        "overall_score": overall_score,
        "overall_explanation": overall_explanation,
    }


def save_submission(user_id, input_text, parsed):
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO submissions (user_id, input_text, clean_text)
            VALUES (%s, %s, %s) RETURNING submission_id
            """,
            (user_id, input_text, parsed["corrected_text"]),
        )
        submission_id = cur.fetchone()[0]

        for err in parsed["spelling_errors"]:
            cur.execute(
                """
                INSERT INTO corrections (submission_id, error_type, corrected_text, explanation)
                VALUES (%s, %s, %s, %s)
                """,
                (submission_id, "spelling", f"{err['wrong']} → {err['correct']}", err["explanation"]),
            )

        for err in parsed["grammar_errors"]:
            cur.execute(
                """
                INSERT INTO corrections (submission_id, error_type, corrected_text, explanation)
                VALUES (%s, %s, %s, %s)
                """,
                (submission_id, "grammar", f"{err['wrong']} → {err['correct']}", err["explanation"]),
            )

        cur.execute(
            """
            INSERT INTO scores (submission_id, spelling_score, grammar_score, sentence_score, overall_score)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (submission_id, parsed["spelling_score"], parsed["grammar_score"],
             parsed["sentence_score"], parsed["overall_score"]),
        )

        conn.commit()
        print(f"[DB] Saved submission_id={submission_id} for user_id={user_id} "
              f"({len(parsed['spelling_errors'])} spelling, {len(parsed['grammar_errors'])} grammar)")
        return submission_id

    finally:
        cur.close()
        conn.close()


# =========================
# SEND MESSAGE TO OLLAMA
# =========================

# A worked example of correcting a FULL multi-clause paragraph without
# shortening it. Sent only for longer input (see build_prompt below) -- the
# fine-tuned model has a strong bias toward falling back to a short,
# memorized answer once a sentence has several embedded clauses, and this
# primes it, at inference time, toward actually correcting the whole thing
# instead. This does not touch or override the model's baked-in Modelfile
# system prompt -- it's just part of the user-turn text we send.
PARAGRAPH_FEWSHOT_EXAMPLE = (
    "எடுத்துக்காட்டு:\n"
    "தவறான வாக்கியம்: நான் காலையில் எழுந்து பல் துலக்கினேன், பிறகு சாப்பாடு "
    "சாப்பிட்டேன், ஆனால் பள்ளிக்கு தாமதமாக போனேன் ஏனெனில் பேருந்து நேரம் "
    "தவறிவிட்டேன்.\n"
    "Corrected Tamil Sentence: நான் காலையில் எழுந்து பல் துலக்கினேன், பிறகு "
    "சாப்பாடு சாப்பிட்டேன், ஆனால் பள்ளிக்கு தாமதமாக சென்றேன், ஏனெனில் "
    "பேருந்து நேரத்தை தவறவிட்டேன்.\n\n"
)

# Below this word count, the plain short prompt already works reliably --
# adding the few-shot example here would just be unnecessary extra context
# (and extra tokens/latency) for something that isn't broken.
PARAGRAPH_FEWSHOT_THRESHOLD_WORDS = 15


def build_prompt(user_message):
    """Short input: the plain prompt that's already proven reliable.
    Longer/multi-clause input: prime the model with a full-paragraph
    worked example first, then explicitly forbid shortening the answer.
    This is a best-effort mitigation, not a guarantee -- a small
    fine-tuned model can still fall back to a short answer on unusual or
    very long paragraphs. Real reliability on paragraphs would need the
    model itself fine-tuned on paragraph-length examples."""
    word_count = len(user_message.split())

    if word_count <= PARAGRAPH_FEWSHOT_THRESHOLD_WORDS:
        return (
            f"{user_message}\n\n"
            "Reply with only the corrected Tamil sentence, in the form:\n"
            "Corrected Tamil Sentence: <the corrected sentence>"
        )

    return (
        PARAGRAPH_FEWSHOT_EXAMPLE
        + "மேலே உள்ள எடுத்துக்காட்டு போல, கீழே உள்ள முழு வாக்கியத்தையும் "
        "(அதன் எல்லா பகுதிகளையும், குறுக்காமல்) திருத்தி எழுதவும். இதை ஒரு "
        "சிறிய வாக்கியமாக சுருக்கக்கூடாது; திருத்தப்பட்ட பதில் மூல "
        "வாக்கியத்தைப் போலவே நீளமாகவும் அனைத்து பகுதிகளையும் "
        "உள்ளடக்கியதாகவும் இருக்க வேண்டும்.\n\n"
        f"தவறான வாக்கியம்: {user_message}\n\n"
        "Corrected Tamil Sentence:"
    )


def call_ollama_once(user_message, attempt=1):
    """Ask the model for exactly the one thing it's proven to do reliably:
    a corrected version of the sentence/paragraph, in its own natural reply
    style. We no longer pass an explicit "system" field -- `ollama create`
    already bakes the Modelfile's SYSTEM block into the model itself, so
    every /api/generate call already includes it automatically.

    num_predict scales with input length so short sentences stay fast and
    long paragraphs aren't cut off mid-correction. Timing is logged so you
    can see, per request, whether the slowness is model load time, prompt
    processing, or token generation -- rather than guessing.

    attempt: with temperature=0 and a fixed seed, decoding is fully
    deterministic, so a retry on the exact same options would just get the
    exact same (wrong) answer again -- pure wasted latency. On attempt 2+
    we nudge the seed and allow a small amount of sampling temperature so a
    retry actually has a chance at a different, hopefully better, result."""
    prompt = build_prompt(user_message)

    # Lowered from *20/max 256 -- that was asking for up to 300 tokens on an
    # ordinary 15-word sentence, which is more generation time than a single
    # sentence correction needs. Paragraphs still get room to breathe.
    input_words = len(user_message.split())
    num_predict = min(768, max(150, input_words * 12))

    options = dict(OLLAMA_BASE_OPTIONS)
    options["num_predict"] = num_predict

    if attempt > 1:
        options["seed"] = OLLAMA_BASE_OPTIONS.get("seed", 42) + attempt
        options["temperature"] = 0.3
        options["top_p"] = 0.9
        options["top_k"] = 40

    request_started = time.monotonic()

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": options,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
        timeout=240,
    )

    wall_seconds = time.monotonic() - request_started

    print("Ollama status:", response.status_code)
    print(f"Ollama wall time: {wall_seconds:.2f}s (num_predict={num_predict}, input_words={input_words})")
    print("Ollama raw response:", response.text[:1000])

    response.raise_for_status()
    result = response.json()

    # Ollama reports its own internal timings in nanoseconds when
    # stream=False. load_duration is large the first time (or after
    # keep_alive expires) because it's reloading the model into memory;
    # eval_duration is actual token generation and scales with num_predict.
    # These print lines are the fastest way to tell which one is the
    # bottleneck on your machine.
    if "total_duration" in result:
        to_ms = lambda ns: ns / 1_000_000
        print(
            "Ollama internal timings (ms): "
            f"total={to_ms(result.get('total_duration', 0)):.0f} "
            f"load={to_ms(result.get('load_duration', 0)):.0f} "
            f"prompt_eval={to_ms(result.get('prompt_eval_duration', 0)):.0f} "
            f"generate={to_ms(result.get('eval_duration', 0)):.0f} "
            f"(generated {result.get('eval_count', 0)} tokens)"
        )

    return result.get("response", "").strip(), result


@app.route("/api/send", methods=["POST"])
def api_send():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "empty message"}), 400

    print("\n========== AI REQUEST ==========")
    print("User message:", user_message)

    request_started = time.monotonic()

    try:
        reply = ""
        result = {}
        parsed = None

        for attempt in range(1, MAX_OLLAMA_ATTEMPTS + 1):
            reply, result = call_ollama_once(user_message, attempt=attempt)
            print(f"Attempt {attempt} reply:", reply)

            parsed = compute_result(user_message, reply)
            if parsed["parsed_ok"]:
                break
            print(f"Attempt {attempt} failed to parse -- retrying" if attempt < MAX_OLLAMA_ATTEMPTS else
                  f"Attempt {attempt} failed to parse -- giving up, no attempts left")

        total_wall = time.monotonic() - request_started
        print(f"/api/send total wall time: {total_wall:.2f}s across up to {MAX_OLLAMA_ATTEMPTS} attempt(s)")
        print("================================\n")

        if parsed is None:
            return jsonify({"error": "Ollama returned an empty response.", "ollama_response": result}), 500

        submission_id = save_submission(session["user_id"], user_message, parsed)

        return jsonify({
            "reply": reply,
            "parsed_ok": parsed["parsed_ok"],
            "submission_id": submission_id,
            "corrected_text": parsed["corrected_text"],
            "spelling_errors": parsed["spelling_errors"],
            "grammar_errors": parsed["grammar_errors"],
            "overall_explanation": parsed["overall_explanation"],
            "scores": {
                "spelling_score": parsed["spelling_score"],
                "grammar_score": parsed["grammar_score"],
                "sentence_score": parsed["sentence_score"],
                "overall_score": parsed["overall_score"],
            },
        })

    except requests.exceptions.ConnectionError as e:
        print("OLLAMA CONNECTION ERROR:", e)
        return jsonify({"error": "Ollama is not running or cannot be reached."}), 500

    except requests.exceptions.Timeout as e:
        print("OLLAMA TIMEOUT:", e)
        return jsonify({"error": "Tamil AI took too long to respond."}), 500

    except requests.exceptions.HTTPError as e:
        print("OLLAMA HTTP ERROR:", e)
        return jsonify({"error": f"Ollama error: {e}"}), 500

    except Exception as e:
        print("UNEXPECTED ERROR:", repr(e))
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/submissions")
def api_submissions():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            """
            SELECT submission_id, input_text, clean_text, created_at
            FROM submissions WHERE user_id = %s ORDER BY created_at DESC
            """,
            (session["user_id"],),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return jsonify([dict(row) for row in rows])


@app.route("/api/submission/<int:submission_id>")
def api_submission_detail(submission_id):
    """NEW: returns everything needed to re-render the full result card for
    one past submission -- corrected text, spelling/grammar error lists and
    scores -- so selecting a history item (or reloading the page) doesn't
    lose the structured view down to a bare pair of chat bubbles."""
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            """
            SELECT submission_id, input_text, clean_text
            FROM submissions WHERE submission_id = %s AND user_id = %s
            """,
            (submission_id, session["user_id"]),
        )
        sub = cur.fetchone()
        if not sub:
            return jsonify({"error": "not found"}), 404

        cur.execute(
            "SELECT error_type, corrected_text, explanation FROM corrections WHERE submission_id = %s",
            (submission_id,),
        )
        correction_rows = cur.fetchall()

        cur.execute(
            """
            SELECT spelling_score, grammar_score, sentence_score, overall_score
            FROM scores WHERE submission_id = %s
            """,
            (submission_id,),
        )
        score_row = cur.fetchone() or {}
    finally:
        cur.close()
        conn.close()

    spelling_errors, grammar_errors = [], []
    for row in correction_rows:
        wrong, _, correct = row["corrected_text"].partition(" → ")
        entry = {"wrong": wrong, "correct": correct, "explanation": row["explanation"]}
        target = spelling_errors if row["error_type"] == "spelling" else grammar_errors
        target.append(entry)

    return jsonify({
        "submission_id": sub["submission_id"],
        "input_text": sub["input_text"],
        "corrected_text": sub["clean_text"],
        "parsed_ok": True,
        "spelling_errors": spelling_errors,
        "grammar_errors": grammar_errors,
        "overall_explanation": None,
        "scores": {
            "spelling_score": score_row.get("spelling_score"),
            "grammar_score": score_row.get("grammar_score"),
            "sentence_score": score_row.get("sentence_score"),
            "overall_score": score_row.get("overall_score"),
        },
    })


@app.route("/api/debug/counts")
def api_debug_counts():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port();")
        db_name, host, port = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM submissions;")
        total_submissions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM submissions WHERE user_id = %s;", (session["user_id"],))
        my_submissions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM corrections;")
        total_corrections = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM scores;")
        total_scores = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()

    return jsonify({
        "connected_to_database": db_name,
        "connected_to_host": str(host) if host else "localhost (unix socket)",
        "connected_to_port": port,
        "total_submissions": total_submissions,
        "my_submissions": my_submissions,
        "total_corrections": total_corrections,
        "total_scores": total_scores,
    })


@app.route("/chart-analysis")
def chart_analysis():
    if not session.get("user_id"):
        return redirect(url_for("index"))
    return "Coming soon"


if __name__ == "__main__":
    init_db()
    app.run(debug=True)