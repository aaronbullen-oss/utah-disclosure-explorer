"""
Create and backfill the entity_layer_tags table — used by the Disclosure
Explorer's "Browse by layer" menu to group entities the same way the donor
map's layer checkboxes do.

Source of truth: the "Entity Donated To" column in each layer's *_geocoded.csv
(the same files uploaded to the donor map's Google Sheet tabs), NOT a
hand-maintained name list. This is more reliable than re-deriving entity
lists by hand, and it covers every layer the donor map has -- including LGBT
Causes and Pro-Abortion, which had no build_*.py script with a name list.

A junction table (not a single column) because some entities legitimately
belong to more than one layer, e.g. 'Utah Building & Construction Trades
Council' appears in both the Labor Unions and Construction donor CSVs.

Layer names match the donor map's GAS UI labels exactly (Index.html), so the
Explorer and the map stay vocabulary-consistent: 'Republican Party' and
'Democrat Party', not 'Republicans'/'Democrats'.
"""
import sqlite3
import pandas as pd
import os

DB = r'C:\Users\aaron\utah-disclosures\utah_disclosures.db'
BASE = r'C:\Users\aaron\utah-disclosures'

# layer display name -> source CSV (same files used for the Google Sheet tabs)
LAYER_CSV_MAP = {
    'Republican Party': 'republican_donors_geocoded.csv',
    'Democrat Party':   'actblue_dem_donors_geocoded.csv',
    'LGBT Causes':      'lgbt_donors_geocoded.csv',
    'Pro-Abortion':     'proabortion_donors_geocoded.csv',
    'Banking':          'banking_donors_geocoded.csv',
    'Insurance':        'insurance_donors_geocoded.csv',
    'Real Estate':      'realestate_donors_geocoded.csv',
    'Construction':     'construction_donors_geocoded.csv',
    'Labor Unions':     'labor_union_donors_geocoded.csv',
    'Teachers Unions':  'teacher_union_donors_geocoded.csv',
}

def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS entity_layer_tags (
            entity_id TEXT NOT NULL,
            layer_tag TEXT NOT NULL,
            PRIMARY KEY (entity_id, layer_tag)
        );
        DELETE FROM entity_layer_tags;
    """)

    total = 0
    for layer, csv_name in LAYER_CSV_MAP.items():
        path = os.path.join(BASE, csv_name)
        if not os.path.exists(path):
            print(f'  WARNING [{layer}]: {csv_name} not found, skipping')
            continue

        df = pd.read_csv(path, dtype=str)
        raw = sorted(n for n in df['Entity Donated To'].dropna().unique() if n.strip())

        placeholders = ','.join('?' for _ in raw)
        found = cur.execute(f'SELECT entity_id, name FROM entities WHERE name IN ({placeholders})', raw).fetchall()
        matched_names = {r[1] for r in found}
        unmatched = [n for n in raw if n not in matched_names]

        # A handful of older CSVs (e.g. lgbt_donors_geocoded.csv) combine
        # multiple entity names into one cell as "A; B" for donors who gave
        # to more than one entity in that layer. Only fall back to splitting
        # for values that didn't match whole -- this avoids corrupting names
        # that legitimately contain "&amp;" (which itself contains a ';').
        still_missing = []
        for val in unmatched:
            parts = [p.strip() for p in val.split(';') if p.strip()]
            if len(parts) > 1:
                ph = ','.join('?' for _ in parts)
                sub_found = cur.execute(f'SELECT entity_id, name FROM entities WHERE name IN ({ph})', parts).fetchall()
                if {r[1] for r in sub_found} == set(parts):
                    found.extend(sub_found)
                    continue
            still_missing.append(val)

        if still_missing:
            print(f'  WARNING [{layer}]: not found in DB (name mismatch): {set(still_missing)}')

        cur.executemany(
            'INSERT OR IGNORE INTO entity_layer_tags (entity_id, layer_tag) VALUES (?, ?)',
            [(eid, layer) for eid, _ in found]
        )
        print(f'{layer}: {len(raw)} unique values in CSV, {len(found)} matched in DB')
        total += len(found)

    conn.commit()
    print(f'\nDone. {total} (entity, layer) tags written.')
    conn.close()

if __name__ == '__main__':
    main()
