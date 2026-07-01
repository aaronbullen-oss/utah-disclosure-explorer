"""
Utah Disclosure Explorer — FastAPI backend
Usage: uvicorn app:app --reload
Then open http://localhost:8000
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from difflib import SequenceMatcher
import sqlite3, re, asyncio, urllib.request, os
from pathlib import Path

BASE = "https://disclosures.utah.gov"
_officer_cache: dict[str, list] = {}


def ensure_officer_tables(cur):
    """Create the officers/officer_extra/officer_scrape_status tables.

    Migration: if an `officers` table exists from before the parser fix
    (single flat record per fieldset, no title/occupation/business_address
    columns), drop and rebuild it -- that old data silently dropped extra
    people within a fieldset (see _fetch_officers_sync docstring), so there's
    nothing worth preserving from it.
    """
    table_exists = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='officers'"
    ).fetchone()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(officers)").fetchall()] if table_exists else []
    if table_exists and "title" not in cols:
        cur.executescript("""
            DROP TABLE IF EXISTS officer_extra;
            DROP TABLE IF EXISTS officers;
            DROP TABLE IF EXISTS officer_scrape_status;
        """)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS officers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id   TEXT NOT NULL,
            role        TEXT,
            name        TEXT,
            title       TEXT,
            address     TEXT,
            phone       TEXT,
            email       TEXT,
            occupation  TEXT,
            business_address TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_officers_entity ON officers(entity_id);
        CREATE TABLE IF NOT EXISTS officer_extra (
            officer_id INTEGER NOT NULL REFERENCES officers(id),
            label      TEXT,
            value      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_officer_extra_officer ON officer_extra(officer_id);
        CREATE TABLE IF NOT EXISTS officer_scrape_status (
            entity_id  TEXT PRIMARY KEY,
            status     TEXT NOT NULL,
            error_msg  TEXT,
            scraped_at TEXT NOT NULL
        );
    """)

DB   = Path(__file__).parent / "utah_disclosures.db"
HTML = Path(__file__).parent / "index.html"

# Google Drive file ID for the SQLite database.
# Set DB_DRIVE_ID env var on Render, or hardcode after uploading.
DB_DRIVE_ID = os.environ.get("DB_DRIVE_ID", "")

_lookup: dict[str, str] = {}   # normalized name -> entity_id
_names:  dict[str, str] = {}   # entity_id -> display name

def _norm(name: str) -> str:
    """Normalize an entity name for fuzzy matching."""
    n = re.sub(r"[',\.\-]", " ", name.lower().strip())
    n = re.sub(r"\b(inc|llc|ltd|co|corp|corporation|incorporated|company|companies|the)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DB.exists():
        if not DB_DRIVE_ID:
            raise RuntimeError("DB not found and DB_DRIVE_ID env var not set")
        print(f"Downloading database from Google Drive ({DB_DRIVE_ID})...")
        import gdown
        gdown.download(id=DB_DRIVE_ID, output=str(DB), quiet=False, fuzzy=True)
        # Verify it's a real SQLite file
        if DB.stat().st_size < 1_000_000:
            DB.unlink()
            raise RuntimeError(f"Downloaded file is too small ({DB.stat().st_size} bytes) — likely a Google Drive warning page, not the DB")
        print(f"Download complete ({DB.stat().st_size / 1e6:.0f} MB).")
    con = sqlite3.connect(DB)
    for eid, name in con.execute("SELECT entity_id, name FROM entities"):
        _names[str(eid)] = name
        _lookup[_norm(name)] = str(eid)
    # Indexes for performance (no-op if already exist)
    con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_t_entity   ON transactions(entity_id);
        CREATE INDEX IF NOT EXISTS idx_t_type     ON transactions(tran_type);
        CREATE INDEX IF NOT EXISTS idx_t_namelc   ON transactions(LOWER(TRIM(name)));
        CREATE INDEX IF NOT EXISTS idx_t_amount   ON transactions(tran_amount);
    """)
    ensure_officer_tables(con.cursor())
    con.commit()
    con.close()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def _search_patterns(q: str) -> list[str]:
    """Return LIKE patterns for a query, adding a reversed 'Last, First' variant
    when the query looks like 'First Last' (two words, no comma)."""
    patterns = [f"%{q}%"]
    parts = q.strip().split()
    if len(parts) == 2 and "," not in q:
        patterns.append(f"%{parts[1]}, {parts[0]}%")
    return patterns


def resolve(name: str) -> str | None:
    """Return entity_id if this donor name matches a known entity, else None."""
    return _lookup.get(_norm(name))


def _name_variants(name: str, con: sqlite3.Connection, threshold: float = 0.92) -> list[str]:
    """Return all donor name variants in the DB that are similar to `name`.

    Uses the first word as a prefix filter to limit candidates, then applies
    SequenceMatcher so minor typos (e.g. 'Goverment' vs 'Government') are caught.
    """
    first_word = re.split(r"\W+", name.strip())[0]
    candidates = con.execute(
        "SELECT DISTINCT name FROM transactions WHERE name LIKE ? AND tran_type='Contribution'",
        (f"{first_word}%",),
    ).fetchall()
    name_lc = name.lower().strip()
    matches = [
        c[0] for c in candidates
        if SequenceMatcher(None, name_lc, c[0].lower().strip()).ratio() >= threshold
    ]
    return matches or [name]


@app.get("/api/entities/browse")
def entities_browse():
    """All entities grouped by category_slug, alphabetical within each group."""
    con = _db()
    rows = con.execute(
        "SELECT entity_id, name, category_slug FROM entities ORDER BY category_slug, name COLLATE NOCASE"
    ).fetchall()
    con.close()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category_slug"] or "OTHER", []).append(
            {"entity_id": str(r["entity_id"]), "name": r["name"]}
        )
    return groups


@app.get("/api/entities/layers")
def entities_layers():
    """All tagged entities grouped by donor-map layer, alphabetical within each group.

    Sourced from entity_layer_tags (see tag_entity_layers.py). LGBT Causes and
    Pro-Abortion are not included — no entity-name list exists for them yet.
    """
    con = _db()
    rows = con.execute("""
        SELECT t.layer_tag, e.entity_id, e.name
        FROM entity_layer_tags t
        JOIN entities e ON e.entity_id = t.entity_id
        ORDER BY t.layer_tag, e.name COLLATE NOCASE
    """).fetchall()
    con.close()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["layer_tag"], []).append(
            {"entity_id": str(r["entity_id"]), "name": r["name"]}
        )
    return groups


@app.get("/api/search")
def search(q: str = "", limit: int = 40):
    if not q:
        return []
    patterns = _search_patterns(q)
    where = " OR ".join("name LIKE ?" for _ in patterns)
    con = _db()
    rows = con.execute(
        f"SELECT entity_id, name, category_slug FROM entities WHERE ({where}) ORDER BY name LIMIT ?",
        patterns + [limit],
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/entity/{entity_id}/donors")
def entity_donors(entity_id: str):
    con = _db()
    ent = con.execute(
        "SELECT entity_id, name, category_slug, folder_id FROM entities WHERE entity_id=?",
        (entity_id,),
    ).fetchone()
    if not ent:
        con.close()
        return {"error": "Entity not found"}

    rows = con.execute("""
        SELECT name,
               SUM(tran_amount) AS total,
               COUNT(*)         AS gifts,
               MIN(tran_date)   AS first_date,
               MAX(address1)    AS addr,
               MAX(city)        AS city,
               MAX(state)       AS state
        FROM   transactions
        WHERE  entity_id = ?
          AND  tran_type = 'Contribution'
          AND  tran_amount > 0
        GROUP  BY LOWER(TRIM(name))
        ORDER  BY total DESC
    """, (entity_id,)).fetchall()
    con.close()

    donors = []
    for r in rows:
        addr = ", ".join(p for p in [r["addr"] or "", r["city"] or "", r["state"] or ""] if p)
        donors.append({
            "name":             r["name"],
            "total":            r["total"],
            "gifts":            r["gifts"],
            "first_date":       r["first_date"],
            "address":          addr,
            "linked_entity_id": resolve(r["name"]),
        })

    return {
        "entity_id":   str(ent["entity_id"]),
        "name":        ent["name"],
        "category":    ent["category_slug"],
        "folder_id":   ent["folder_id"],
        "total_raised": sum(d["total"] for d in donors),
        "donors":      donors,
    }


@app.get("/api/donor/search")
def donor_search(q: str = "", limit: int = 40):
    if not q:
        return []
    patterns = _search_patterns(q)
    where = " OR ".join("name LIKE ?" for _ in patterns)
    con = _db()
    rows = con.execute(f"""
        SELECT name,
               SUM(tran_amount)  AS total,
               COUNT(*)          AS gifts,
               MAX(address1)     AS addr,
               MAX(city)         AS city,
               MAX(state)        AS state
        FROM   transactions
        WHERE  ({where}) AND tran_type = 'Contribution' AND tran_amount > 0
        GROUP  BY LOWER(TRIM(name))
        ORDER  BY total DESC
        LIMIT  ?
    """, patterns + [limit]).fetchall()
    con.close()
    return [{
        "name":             r["name"],
        "total":            r["total"],
        "gifts":            r["gifts"],
        "address":          ", ".join(p for p in [r["addr"] or "", r["city"] or "", r["state"] or ""] if p),
        "linked_entity_id": resolve(r["name"]),
    } for r in rows]


@app.get("/api/donor/given")
def donor_given(name: str):
    con = _db()
    variants = _name_variants(name, con)
    placeholders = ",".join("?" * len(variants))
    rows = con.execute(f"""
        SELECT e.entity_id,
               e.name          AS entity_name,
               e.category_slug AS category,
               SUM(t.tran_amount) AS total,
               COUNT(*)           AS gifts,
               MIN(t.tran_date)   AS first_date
        FROM   transactions t
        JOIN   entities e ON t.entity_id = e.entity_id
        WHERE  t.name IN ({placeholders})
          AND  t.tran_type = 'Contribution'
          AND  t.tran_amount > 0
        GROUP  BY e.entity_id
        ORDER  BY total DESC
    """, variants).fetchall()
    con.close()
    return {
        "name":             name,
        "linked_entity_id": resolve(name),
        "given":            [dict(r) for r in rows],
    }


@app.get("/api/dates")
def transaction_dates(entity_id: str, donor_name: str):
    con = _db()
    rows = con.execute("""
        SELECT tran_date, tran_amount
        FROM   transactions
        WHERE  entity_id = ?
          AND  LOWER(TRIM(name)) = LOWER(TRIM(?))
          AND  tran_type = 'Contribution'
          AND  tran_amount > 0
        ORDER  BY tran_date
    """, (entity_id, donor_name)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _fetch_officers_sync(entity_id: str) -> list[dict]:
    """Fetch officer/registration info from /Registration/EntityDetails/{entity_id}.

    Page structure: <fieldset><legend>Information about the X</legend>
    containing <div class="dis-cell"><label>Field</label>Value</div> rows.

    IMPORTANT: a single fieldset can list MULTIPLE people under one legend
    (e.g. "all other Officers" lists every additional officer one after
    another). The "First" label reliably marks the start of each new
    person's fields, so records are split there rather than treating the
    whole fieldset as one flat dict -- the latter silently overwrote all
    but the last person in any multi-person fieldset.

    A person's block can also contain a "Business Address" sub-block that
    reuses the City/State/Zip/Suite labels from the mailing address; those
    are captured separately (prefixed "Business ...") once "Business
    Address" is seen, so they don't clobber the mailing address fields.

    Fields not otherwise recognized (Title, Occupation, Office, District #,
    Party, County of Election, Date Created, etc. -- these vary by entity
    category) are preserved generically in "extra" so nothing on the
    Statement of Organization is silently dropped.
    """
    url = f"{BASE}/Registration/EntityDetails/{entity_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": str(e)}]

    html = re.sub(r'<(?:script|style)[^>]*>.*?</(?:script|style)>', '', html, flags=re.DOTALL | re.I)

    def strip_tags(s):
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s)).strip()

    KNOWN_LABELS = {
        "First", "Middle", "Last", "Suffix", "Title",
        "Street Address", "Suite/PO Box", "City", "State", "Zip",
        "Telephone Number", "Email", "Occupation",
    }

    def build_officer(role, fields, extra):
        name = " ".join(fields[k] for k in ("First", "Middle", "Last", "Suffix") if fields.get(k))
        addr = ", ".join(filter(None, [
            fields.get("Street Address", ""),
            fields.get("Suite/PO Box", ""),
            fields.get("City", ""),
            " ".join(filter(None, [fields.get("State", ""), fields.get("Zip", "")])),
        ]))
        biz_addr = ", ".join(filter(None, [
            fields.get("Business Street Address", ""),
            fields.get("Business Suite/PO Box", ""),
            fields.get("Business City", ""),
            " ".join(filter(None, [fields.get("Business State", ""), fields.get("Business Zip", "")])),
        ]))
        officer: dict = {"role": role}
        if name:                            officer["name"] = name
        if fields.get("Title"):             officer["title"] = fields["Title"]
        if addr:                            officer["address"] = addr
        if fields.get("Telephone Number"):  officer["phone"] = fields["Telephone Number"]
        if fields.get("Email"):             officer["email"] = fields["Email"]
        if fields.get("Occupation"):        officer["occupation"] = fields["Occupation"]
        if biz_addr:                        officer["business_address"] = biz_addr
        if extra:                           officer["extra"] = extra
        return officer

    officers = []
    for fs in re.finditer(r'<fieldset[^>]*>(.*?)</fieldset>', html, re.DOTALL | re.I):
        fs_html = fs.group(1)
        leg = re.search(r'<legend[^>]*>([^<]+)</legend>', fs_html, re.I)
        if not leg:
            continue
        # Only process fieldsets that describe at least one person
        if not re.search(r'<label[^>]*>\s*Last\s*</label>', fs_html, re.I):
            continue

        role = re.sub(r'(?i)information about (the )?', '', leg.group(1)).strip()

        fields: dict[str, str] = {}
        extra: list[dict[str, str]] = []
        business_mode = False
        started = False

        for cell in re.finditer(
            r'<div[^>]*class="[^"]*dis-cell[^"]*"[^>]*>(.*?)</div>',
            fs_html, re.DOTALL | re.I,
        ):
            cell_html = cell.group(1)
            lbl = re.search(r'<label[^>]*>([^<]+)</label>', cell_html, re.I)
            if not lbl:
                continue
            label = lbl.group(1).strip()
            value = strip_tags(re.sub(r'<label[^>]*>[^<]*</label>', '', cell_html, flags=re.I))

            if label == "First":
                if started:
                    officers.append(build_officer(role, fields, extra))
                fields, extra, business_mode = {}, [], False
                started = True

            if not value:
                continue

            if label == "Business Address":
                business_mode = True
                fields["Business Street Address"] = value
            elif business_mode and label in ("Suite/PO Box", "City", "State", "Zip"):
                fields[f"Business {label}"] = value
            elif label in KNOWN_LABELS:
                fields[label] = value
            else:
                extra.append({"label": label, "value": value})

        if started:
            officers.append(build_officer(role, fields, extra))

    return officers


@app.get("/api/entity/{entity_id}/officers")
async def entity_officers(entity_id: str):
    con = _db()
    ent = con.execute(
        "SELECT name, folder_id FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone()
    if not ent:
        con.close()
        return {"error": "not found", "officers": []}

    filing_url = f"{BASE}/Search/PublicSearch/FolderDetails/{ent['folder_id']}"

    # Prefer the DB (populated by scrape_officers.py) -- instant, and
    # doesn't depend on disclosures.utah.gov being responsive right now.
    status_row = con.execute(
        "SELECT status FROM officer_scrape_status WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if status_row and status_row["status"] == "ok":
        rows = con.execute(
            "SELECT id, role, name, title, address, phone, email, occupation, business_address "
            "FROM officers WHERE entity_id = ?",
            (entity_id,),
        ).fetchall()
        officers = []
        for r in rows:
            o = {"role": r["role"]}
            if r["name"]:              o["name"] = r["name"]
            if r["title"]:             o["title"] = r["title"]
            if r["address"]:           o["address"] = r["address"]
            if r["phone"]:             o["phone"] = r["phone"]
            if r["email"]:             o["email"] = r["email"]
            if r["occupation"]:        o["occupation"] = r["occupation"]
            if r["business_address"]:  o["business_address"] = r["business_address"]
            extras = con.execute(
                "SELECT label, value FROM officer_extra WHERE officer_id = ?", (r["id"],)
            ).fetchall()
            if extras:
                o["extra"] = [{"label": x["label"], "value": x["value"]} for x in extras]
            officers.append(o)
        con.close()
        return {"name": ent["name"], "filing_url": filing_url, "officers": officers}
    con.close()

    # Not scraped yet (or last scrape attempt failed) -- fall back to a
    # live fetch, same as before the batch scraper existed.
    if entity_id in _officer_cache:
        officers = _officer_cache[entity_id]
    else:
        officers = await asyncio.to_thread(_fetch_officers_sync, entity_id)
        # Don't cache transient fetch failures (e.g. upstream timeout) --
        # only cache real results so a later retry can succeed once the
        # state site recovers, instead of being stuck on the cached error
        # for the lifetime of this server process.
        if not (len(officers) == 1 and "error" in officers[0]):
            _officer_cache[entity_id] = officers

    return {
        "name":       ent["name"],
        "filing_url": filing_url,
        "officers":   officers,
    }


@app.get("/")
def index():
    return FileResponse(HTML, headers={"Cache-Control": "no-cache, must-revalidate"})

@app.head("/")
def index_head():
    from fastapi.responses import Response
    return Response()
