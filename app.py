from __future__ import annotations

import os
import sqlite3
import string
from functools import wraps
from pathlib import Path
from random import choices

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "markets.db"))
STARTING_BALANCE = 1000.0
YES_NO_CHOICES = {"YES", "NO"}


app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY") or "local-dev-secret"
app.config["SECRET_KEY"] = secret_key
app.secret_key = secret_key


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error: Exception | None) -> None:
    database = g.pop("db", None)
    if database is not None:
        database.close()


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_PATH)
    cursor = database.cursor()
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            username TEXT UNIQUE,
            password_hash TEXT,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            question TEXT NOT NULL,
            entry_price REAL NOT NULL,
            creator_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            resolved_outcome TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            FOREIGN KEY (creator_id) REFERENCES players (id)
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            stake REAL NOT NULL,
            payout REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (market_id, player_id),
            FOREIGN KEY (market_id) REFERENCES markets (id),
            FOREIGN KEY (player_id) REFERENCES players (id)
        );
        """
    )
    ensure_player_columns(database)
    database.commit()
    database.close()


def ensure_player_columns(database: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in database.execute("PRAGMA table_info(players)").fetchall()
    }
    if "username" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN username TEXT")
    if "password_hash" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN password_hash TEXT")
    database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_players_username ON players(username)"
    )


def generate_market_code(database: sqlite3.Connection) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(choices(alphabet, k=6))
        existing = database.execute("SELECT 1 FROM markets WHERE code = ?", (code,)).fetchone()
        if existing is None:
            return code


def normalize_name(raw_name: str) -> str:
    return " ".join(raw_name.strip().split())


def normalize_username(raw_username: str) -> str:
    return raw_username.strip().lower()


def parse_side(raw_side: str) -> str | None:
    side = raw_side.strip().upper()
    return side if side in YES_NO_CHOICES else None


def parse_price(raw_price: str) -> float | None:
    try:
        price = float(raw_price)
    except ValueError:
        return None
    if price <= 0:
        return None
    return round(price, 2)


def get_player_by_id(player_id: int | None) -> sqlite3.Row | None:
    if player_id is None:
        return None
    return get_db().execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()


def get_player_by_username(database: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return database.execute("SELECT * FROM players WHERE username = ?", (username,)).fetchone()


def create_account(display_name: str, username: str, password: str) -> sqlite3.Row:
    database = get_db()
    normalized_name = normalize_name(display_name)
    normalized_username = normalize_username(username)

    if not normalized_name or not normalized_username or not password:
        raise ValueError("Name, username, and password are required.")

    existing_name = database.execute("SELECT 1 FROM players WHERE name = ?", (normalized_name,)).fetchone()
    if existing_name is not None:
        raise ValueError("That display name is already taken.")

    existing_username = get_player_by_username(database, normalized_username)
    if existing_username is not None:
        raise ValueError("That username is already taken.")

    cursor = database.execute(
        "INSERT INTO players (name, username, password_hash, balance) VALUES (?, ?, ?, ?)",
        (normalized_name, normalized_username, generate_password_hash(password), STARTING_BALANCE),
    )
    database.commit()
    return database.execute("SELECT * FROM players WHERE id = ?", (cursor.lastrowid,)).fetchone()


def authenticate_user(username: str, password: str) -> sqlite3.Row:
    database = get_db()
    normalized_username = normalize_username(username)
    player = get_player_by_username(database, normalized_username)
    if player is None or not player["password_hash"]:
        raise ValueError("Invalid username or password.")
    if not check_password_hash(player["password_hash"], password):
        raise ValueError("Invalid username or password.")
    return player


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Log in to use the market.", "error")
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user() -> None:
    g.user = get_player_by_id(session.get("player_id"))


def debit_player(database: sqlite3.Connection, player_id: int, amount: float) -> None:
    database.execute(
        "UPDATE players SET balance = balance - ? WHERE id = ?",
        (amount, player_id),
    )


def credit_player(database: sqlite3.Connection, player_id: int, amount: float) -> None:
    database.execute(
        "UPDATE players SET balance = balance + ? WHERE id = ?",
        (amount, player_id),
    )


def create_market(question: str, creator_id: int, creator_side: str, entry_price: float) -> str:
    database = get_db()
    creator = database.execute("SELECT * FROM players WHERE id = ?", (creator_id,)).fetchone()
    if creator is None:
        raise ValueError("Creator account not found.")
    if creator["balance"] < entry_price:
        raise ValueError(f"{creator['name']} does not have enough balance to create this market.")

    code = generate_market_code(database)
    cursor = database.execute(
        "INSERT INTO markets (code, question, entry_price, creator_id) VALUES (?, ?, ?, ?)",
        (code, question.strip(), entry_price, creator["id"]),
    )
    debit_player(database, creator["id"], entry_price)
    database.execute(
        "INSERT INTO positions (market_id, player_id, side, stake) VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, creator["id"], creator_side, entry_price),
    )
    database.commit()
    return code


def join_market(market_code: str, player_id: int, side: str) -> None:
    database = get_db()
    market = database.execute(
        "SELECT * FROM markets WHERE code = ?",
        (market_code.strip().upper(),),
    ).fetchone()
    if market is None:
        raise ValueError("Market code not found.")
    if market["status"] != "open":
        raise ValueError("That market is already resolved.")

    player = database.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if player is None:
        raise ValueError("Player account not found.")
    existing_position = database.execute(
        "SELECT 1 FROM positions WHERE market_id = ? AND player_id = ?",
        (market["id"], player["id"]),
    ).fetchone()
    if existing_position is not None:
        raise ValueError(f"{player['name']} has already joined this market.")
    if player["balance"] < market["entry_price"]:
        raise ValueError(f"{player['name']} does not have enough balance to join.")

    debit_player(database, player["id"], market["entry_price"])
    database.execute(
        "INSERT INTO positions (market_id, player_id, side, stake) VALUES (?, ?, ?, ?)",
        (market["id"], player["id"], side, market["entry_price"]),
    )
    database.commit()


def resolve_market(market_id: int, resolver_id: int, outcome: str) -> None:
    database = get_db()
    market = database.execute(
        """
        SELECT markets.*, players.name AS creator_name
        FROM markets
        JOIN players ON players.id = markets.creator_id
        WHERE markets.id = ?
        """,
        (market_id,),
    ).fetchone()
    if market is None:
        raise ValueError("Market not found.")
    if market["status"] != "open":
        raise ValueError("This market was already resolved.")

    if resolver_id != market["creator_id"]:
        raise ValueError("Only the market creator can resolve this question.")

    positions = database.execute(
        "SELECT * FROM positions WHERE market_id = ?",
        (market_id,),
    ).fetchall()
    if not positions:
        raise ValueError("This market has no participants.")

    winners = [position for position in positions if position["side"] == outcome]
    total_pot = sum(position["stake"] for position in positions)

    if winners:
        winner_stake_total = sum(position["stake"] for position in winners)
        for winner in winners:
            payout = round(total_pot * (winner["stake"] / winner_stake_total), 2)
            credit_player(database, winner["player_id"], payout)
            database.execute(
                "UPDATE positions SET payout = ? WHERE id = ?",
                (payout, winner["id"]),
            )
    else:
        for position in positions:
            credit_player(database, position["player_id"], position["stake"])
            database.execute(
                "UPDATE positions SET payout = ? WHERE id = ?",
                (position["stake"], position["id"]),
            )

    database.execute(
        """
        UPDATE markets
        SET status = 'resolved', resolved_outcome = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (outcome, market_id),
    )
    database.commit()


def fetch_players() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM players ORDER BY balance DESC, name ASC"
    ).fetchall()


def fetch_market_card_by_code(market_code: str) -> dict | None:
    database = get_db()
    market = database.execute(
        """
        SELECT markets.*, players.name AS creator_name
        FROM markets
        JOIN players ON players.id = markets.creator_id
        WHERE markets.code = ?
        """,
        (market_code.strip().upper(),),
    ).fetchone()
    if market is None:
        return None

    positions = database.execute(
        """
        SELECT positions.*, players.name AS player_name
        FROM positions
        JOIN players ON players.id = positions.player_id
        WHERE positions.market_id = ?
        ORDER BY positions.created_at ASC
        """,
        (market["id"],),
    ).fetchall()
    return build_market_card(market, positions)


def build_market_card(market: sqlite3.Row, positions: list[sqlite3.Row]) -> dict:
    yes_count = sum(1 for position in positions if position["side"] == "YES")
    no_count = sum(1 for position in positions if position["side"] == "NO")
    total_pot = sum(position["stake"] for position in positions)
    return {
        "market": market,
        "positions": positions,
        "yes_count": yes_count,
        "no_count": no_count,
        "total_pot": total_pot,
    }


def fetch_market_cards() -> list[dict]:
    database = get_db()
    markets = database.execute(
        """
        SELECT markets.*, players.name AS creator_name
        FROM markets
        JOIN players ON players.id = markets.creator_id
        ORDER BY CASE WHEN markets.status = 'open' THEN 0 ELSE 1 END, markets.created_at DESC
        """
    ).fetchall()

    market_cards: list[dict] = []
    for market in markets:
        positions = database.execute(
            """
            SELECT positions.*, players.name AS player_name
            FROM positions
            JOIN players ON players.id = positions.player_id
            WHERE positions.market_id = ?
            ORDER BY positions.created_at ASC
            """,
            (market["id"],),
        ).fetchall()

        market_cards.append(build_market_card(market, positions))

    return market_cards


def get_invite_url(market_code: str) -> str:
    return url_for("market_detail", market_code=market_code, _external=True)


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        players=fetch_players(),
        market_cards=fetch_market_cards(),
        starting_balance=STARTING_BALANCE,
        selected_market=None,
        invite_url=None,
    )


@app.route("/market/<market_code>", methods=["GET"])
def market_detail(market_code: str):
    card = fetch_market_card_by_code(market_code)
    if card is None:
        flash("That invite link does not match a market.", "error")
        return redirect(url_for("index"))

    return render_template(
        "index.html",
        players=fetch_players(),
        market_cards=fetch_market_cards(),
        starting_balance=STARTING_BALANCE,
        selected_market=card,
        invite_url=get_invite_url(card["market"]["code"]),
    )


@app.route("/register", methods=["POST"])
def register_route():
    display_name = request.form.get("display_name", "")
    username = request.form.get("username", "")
    password = request.form.get("password", "")

    try:
        player = create_account(display_name, username, password)
    except ValueError as error:
        flash(str(error), "error")
    else:
        session["player_id"] = player["id"]
        flash("Account created. You are logged in.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/login", methods=["POST"])
def login_route():
    username = request.form.get("username", "")
    password = request.form.get("password", "")

    try:
        player = authenticate_user(username, password)
    except ValueError as error:
        flash(str(error), "error")
    else:
        session["player_id"] = player["id"]
        flash(f"Logged in as {player['name']}.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/logout", methods=["POST"])
def logout_route():
    session.clear()
    flash("You are logged out.", "success")
    return redirect(url_for("index"))


@app.route("/create-market", methods=["POST"])
@login_required
def create_market_route():
    question = request.form.get("question", "").strip()
    creator_side = parse_side(request.form.get("creator_side", ""))
    entry_price = parse_price(request.form.get("entry_price", ""))

    if not question or creator_side is None or entry_price is None:
        flash("Enter a yes/no question, a side, and a valid entry price.", "error")
        return redirect(url_for("index"))

    try:
        market_code = create_market(question, g.user["id"], creator_side, entry_price)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))

    flash("Market created. Share the invite link with friends so they can join.", "success")
    return redirect(url_for("market_detail", market_code=market_code))


@app.route("/join-market", methods=["POST"])
@login_required
def join_market_route():
    market_code = request.form.get("market_code", "")
    side = parse_side(request.form.get("side", ""))

    if not market_code.strip() or side is None:
        flash("Enter the market code and whether you are joining YES or NO.", "error")
        return redirect(request.referrer or url_for("index"))

    try:
        join_market(market_code, g.user["id"], side)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("You joined the market.", "success")
    return redirect(url_for("market_detail", market_code=market_code.strip().upper()))


@app.route("/resolve-market", methods=["POST"])
@login_required
def resolve_market_route():
    outcome = parse_side(request.form.get("outcome", ""))

    try:
        market_id = int(request.form.get("market_id", ""))
    except ValueError:
        flash("Invalid market selection.", "error")
        return redirect(request.referrer or url_for("index"))

    if outcome is None:
        flash("Choose the winning outcome.", "error")
        return redirect(request.referrer or url_for("index"))

    try:
        resolve_market(market_id, g.user["id"], outcome)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Market resolved and balances updated.", "success")
    return redirect(request.referrer or url_for("index"))


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))