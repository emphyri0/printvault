#!/usr/bin/env python3
"""Generates bambu_errors_data.json from the error code lists of ha-bambulab.

Source: https://github.com/greghesp/ha-bambulab (MIT licence), folder scripts/hms_error_text/.
ha-bambulab gets those texts from Bambu Lab itself (https://e.bambulab.com/query.php, the same
source as Bambu Studio) and the wiki links from https://wiki.bambulab.com/en/hms/home.

Per code, the text for the chosen printer model (P2S by default) is used when there is a
model-specific variant, else the general text. Language English by default; for another language,
English is the fallback. The same goes for the wiki link.

Also the stage texts (print.stg_cur): the CURRENT_STAGE_IDS table from pybambu/const.py and the
names from translations/<lang>.json and en.json. const.py is read with ast.literal_eval (only that
one literal dict), never executed.

The other files are only read as JSON; nothing from the download is executed.

Usage (from the project folder):
    python3 scripts/update_bambu_errors.py                 # latest version of main
    python3 scripts/update_bambu_errors.py --ref <commit>  # a fixed version
    python3 scripts/update_bambu_errors.py --model H2D --lang nl
"""
import argparse
import ast
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "greghesp/ha-bambulab"
DATA_PATH = "scripts/hms_error_text"
CONST_PATH = "custom_components/bambu_lab/pybambu/const.py"
TRANSLATIONS_PATH = "custom_components/bambu_lab/translations"
WIKI_BASE = "https://wiki.bambulab.com"
OUT = Path(__file__).resolve().parent.parent / "bambu_errors_data.json"
MAX_BYTES = 20 * 1024 * 1024


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "printvault-update-script",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        sys.exit(f"Response too large: {url}")
    return body


def fetch(url):
    return json.loads(fetch_bytes(url))


def literal_dict(source, name):
    """The literal dict `name = {...}` from Python source code, without executing that code."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, dict):
                return value
    sys.exit(f"{name} not found in {CONST_PATH}")


def build_stages(stage_ids, texts, fallback):
    """{"0": {"key": "printing", "en": "Printing", "text": "<chosen language>"}}; 'default' and non-integer codes are dropped."""
    def names(tr):
        return (((tr.get("entity") or {}).get("sensor") or {}).get("stage") or {}).get("state") or {}
    local, en = names(texts), names(fallback)
    out = {}
    for code, key in stage_ids.items():
        if isinstance(code, int) and isinstance(key, str):
            out[str(code)] = {"key": key, "en": en.get(key), "text": local.get(key) or en.get(key)}
    return out


def resolve_ref(ref):
    """Branch or tag → fixed commit sha, so the source is pinned exactly."""
    commit = fetch(f"https://api.github.com/repos/{REPO}/commits/{ref}")
    return commit["sha"], commit["commit"]["committer"]["date"]


def pick(variants, model):
    """{text: [models]} → text for `model`, else the general one (empty model list)."""
    if not isinstance(variants, dict) or not variants:
        return None
    for value, models in variants.items():
        if model in models:
            return value
    for value, models in variants.items():
        if not models:
            return value
    return next(iter(variants))


def fmt_error(code):
    return f"{code[:4]}-{code[4:]}"                                     # 0300400C → 0300-400C


def fmt_hms(code):
    return "_".join(code[i:i + 4] for i in range(0, 16, 4))            # 0300…0003 → 0300_0100_0001_0003


def valid(code, length):
    return isinstance(code, str) and len(code) == length and all(c in "0123456789ABCDEF" for c in code.upper())


def build(texts, fallback, wiki, model):
    out = {"print_errors": {}, "hms": {}, "wiki": {}}
    for key, length, fmt, target in (("device_error", 8, fmt_error, "print_errors"),
                                     ("device_hms", 16, fmt_hms, "hms")):
        codes = set(texts.get(key, {})) | set(fallback.get(key, {}))
        for code in sorted(codes):
            if not valid(code, length):
                continue
            text = pick(texts.get(key, {}).get(code), model) or pick(fallback.get(key, {}).get(code), model)
            if text and text.strip():
                out[target][fmt(code.upper())] = text.strip()
    for code, variants in sorted(wiki.items()):
        url = pick(variants, model)
        if valid(code, 16) and isinstance(url, str) and url.startswith("/en/"):
            out["wiki"][fmt_hms(code.upper())] = WIKI_BASE + url
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="main", help="branch, tag or commit of ha-bambulab (default: main)")
    ap.add_argument("--model", default="P2S", help="printer model for model-specific texts (default: P2S)")
    ap.add_argument("--lang", default="en", help="language (default: en; English is the fallback)")
    args = ap.parse_args()

    sha, date = resolve_ref(args.ref)
    raw = f"https://raw.githubusercontent.com/{REPO}/{sha}/{DATA_PATH}"
    print(f"ha-bambulab {sha[:12]} ({date})")
    texts = fetch(f"{raw}/hms_{args.lang}.json")
    fallback = fetch(f"{raw}/hms_en.json") if args.lang != "en" else {}
    wiki = fetch(f"{raw}/wiki_links.json")
    base = f"https://raw.githubusercontent.com/{REPO}/{sha}"
    stage_ids = literal_dict(fetch_bytes(f"{base}/{CONST_PATH}").decode("utf-8"), "CURRENT_STAGE_IDS")
    tr = fetch(f"{base}/{TRANSLATIONS_PATH}/{args.lang}.json")
    tr_en = fetch(f"{base}/{TRANSLATIONS_PATH}/en.json") if args.lang != "en" else tr

    data = build(texts, fallback, wiki, args.model)
    data["stages"] = build_stages(stage_ids, tr, tr_en)
    data["_source"] = {
        "repo": f"https://github.com/{REPO}",
        "commit": sha,
        "commit_date": date,
        "files": [f"{DATA_PATH}/hms_{args.lang}.json", f"{DATA_PATH}/hms_en.json", f"{DATA_PATH}/wiki_links.json",
                  f"{CONST_PATH} (CURRENT_STAGE_IDS)", f"{TRANSLATIONS_PATH}/{args.lang}.json", f"{TRANSLATIONS_PATH}/en.json"],
        "license": "MIT (ha-bambulab contributors); texts by Bambu Lab (e.bambulab.com)",
        "model": args.model,
        "lang": args.lang,
        "generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "generator": "scripts/update_bambu_errors.py",
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(data['print_errors'])} print_error codes, {len(data['hms'])} HMS codes, "
          f"{len(data['wiki'])} wiki links, {len(data['stages'])} stages → {OUT.name}")


if __name__ == "__main__":
    main()
