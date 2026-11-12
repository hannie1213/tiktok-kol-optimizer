"""
Build the default seed creator library (data/sample_kols.json) from your own
real Excel/CSV exports (e.g. FastMoss).

This REPLACES sample_kols.json with exactly the rows in the files you pass —
it does not append to whatever is already there. The result becomes the
template library every new merchant tenant is provisioned from
(tenancy.TEMPLATE_SEED = data/seed_kols.json), and what /kols/reset returns to.

It runs the same validation + header-alias + number/rate-parsing + cost-estimate
pipeline as the live import endpoint, so messy headers ("Follower Count",
"Engagement Rate (%)", "Region"), formatted numbers ("250K", "$1,200", "8%"),
and country/category names are all normalised.

Category refinement: FastMoss exports carry a `selling_categories` column
(e.g. "Electronics, Home & Living, Fashion") that is finer-grained than the
coarse `category` column. Each creator is re-classified to the first entry of
`selling_categories` that maps to a known category, so creators land in the
full taxonomy (electronics, mother_baby, health, ...) instead of being
collapsed into the four legacy verticals. Rows without a usable
`selling_categories` keep their `category` column. Pass --no-refine to trust
the `category` column as-is.

Usage (from project root, using the project's python):
    .conda\\python.exe data\\build_seed.py data\\fastmoss\\*.xlsx
    .conda\\python.exe data\\build_seed.py data\\Beauty_MY.xlsx data\\Fashion_ID.xlsx
    # force a default when a file lacks country/category columns AND the
    # filename can't be inferred:
    .conda\\python.exe data\\build_seed.py myfile.xlsx --country MY --category beauty

Per-file country/category are taken from (in order): an explicit column in the
file → the filename (e.g. "Beauty_MY.xlsx") → the --country/--category flags.
"""
import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import crud


def _infer_defaults_from_name(path, cli_country, cli_category):
    stem_tokens = set(crud._norm_header(os.path.splitext(os.path.basename(path))[0]).split("_"))
    category = cli_category or next(
        (crud._CATEGORY_SYNONYMS[t] for t in stem_tokens if t in crud._CATEGORY_SYNONYMS), None
    )
    country = cli_country or next(
        (crud._COUNTRY_NAME_TO_CODE[t] for t in stem_tokens if t in crud._COUNTRY_NAME_TO_CODE), None
    )
    return country, category


def _read_rows(path):
    with open(path, "rb") as f:
        content = f.read()
    lower = path.lower()
    if lower.endswith(".csv"):
        return crud._read_csv_rows(content)
    if lower.endswith(".xlsx"):
        return crud._read_excel_rows(content)
    raise SystemExit(f"Unsupported file (need .csv or .xlsx): {path}")


def _refine_category(row):
    """Primary category from the FastMoss `selling_categories` column, or None."""
    for token in str(row.get("selling_categories") or "").split(","):
        cat = crud._CATEGORY_SYNONYMS.get(crud._norm_header(token))
        if cat in crud.VALID_CATEGORIES:
            return cat
    return None


def main():
    ap = argparse.ArgumentParser(description="Build sample_kols.json from real exports.")
    ap.add_argument("files", nargs="+", help="One or more .xlsx/.csv files (globs allowed).")
    ap.add_argument("--country", default=None, help="Fallback country code for files lacking one.")
    ap.add_argument("--category", default=None, help="Fallback category for files lacking one.")
    ap.add_argument("--no-refine", action="store_true",
                    help="Do not re-classify from selling_categories; trust the category column.")
    args = ap.parse_args()

    paths = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        if not matched:
            print(f"  ! no match for: {pattern}")
        paths.extend(sorted(matched))
    if not paths:
        raise SystemExit("No input files found.")

    library, grand_estimated, grand_errors = [], 0, []
    for path in paths:
        headers, raw_rows = _read_rows(path)
        rows = crud._canonicalize_rows(headers, raw_rows)

        refined = 0
        if not args.no_refine:
            for row in rows:
                cat = _refine_category(row)
                if cat:
                    old = crud._CATEGORY_SYNONYMS.get(crud._norm_header(str(row.get("category") or "")))
                    refined += old != cat
                    row["category"] = cat

        country, category = _infer_defaults_from_name(path, args.country, args.category)
        records, estimated, errors = crud._rows_to_records(rows, country, category)
        for rec in records:
            rec["source"] = "fastmoss"
        library.extend(records)
        grand_estimated += estimated
        grand_errors.extend(f"{os.path.basename(path)}: {e}" for e in errors)
        print(f"  + {os.path.basename(path):<28} added={len(records):<4} reclassified={refined:<4} "
              f"cost-estimated={estimated:<4} errors={len(errors)} "
              f"(defaults country={country or '—'} category={category or '—'})")

    library = [{"id": i, **rec} for i, rec in enumerate(library, start=1)]

    # Write the live library and snapshot it as the restore baseline / template
    # seed that new tenants and /kols/reset start from.
    for path in (crud.DATA_PATH, crud.SEED_PATH):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=2, ensure_ascii=False)

    print(f"\nSeed written to {crud.DATA_PATH}")
    print(f"Restore baseline written to {crud.SEED_PATH}")
    print(f"  total creators: {len(library)}")
    if grand_estimated:
        print(f"  cost estimated from followers for {grand_estimated} rows "
              f"(no price in source — replace with negotiated quotes later)")

    print("  category distribution:")
    for cat, n in collections.Counter(k["category"] for k in library).most_common():
        print(f"    {cat:<16} {n}")

    if grand_errors:
        print(f"  {len(grand_errors)} rows skipped:")
        for e in grand_errors[:30]:
            print(f"    - {e}")
        if len(grand_errors) > 30:
            print(f"    ... and {len(grand_errors) - 30} more")


if __name__ == "__main__":
    main()
