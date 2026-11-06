# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Local-only Flask prototype for managing construction (MEP) inspection defects. Site staff photograph a whiteboard next to each defect; an OpenAI vision model reads the board and the app either creates a new defect, attaches an "after improvement" (改善後) photo to an existing one, or routes it to a manual-review queue. UI text, DB status values, and the vision prompt are Traditional Chinese (Taiwan); whiteboard dates may be ROC calendar (民國115年 = 2026). User docs: `README_使用說明.md`.

## Commands

```bash
# host lacks python3-venv/ensurepip, so build the venv with uv
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python app.py             # serves http://127.0.0.1:5000 (PORT from .env)
bash start_mac_linux.sh             # copies .env.example -> .env if missing, installs deps, runs
```

There are no tests, linter, or build step. Validate via the HTTP API:

```bash
curl http://127.0.0.1:5000/api/defects
curl -F photos=@photo.jpg http://127.0.0.1:5000/api/upload
curl -o out.xlsx http://127.0.0.1:5000/api/export.xlsx
```

Config (`.env`, never commit or print it): `OPENAI_API_KEY` (optional), `OPENAI_MODEL` (default `gpt-4.1-mini`), `PORT`.

## Architecture

Everything lives in `app.py` (backend) and `templates/index.html` (single-page frontend calling `/api/*`).

- **Storage**: SQLite at `data/defects.sqlite3`, photos in `data/images/`. `init_db()` runs at import time; if `defects` is empty it seeds 32 records from `seed.json` + `seed_images/`. Resetting means deleting `data/`, which holds user data — confirm first.
- **Tables**: `defects` (one row per defect; `public_id` like `MEP-20261104-021`; same-day duplicate board numbers get `-2`, `-3` suffixes via `issue_id()`), `submissions` (every uploaded photo + raw AI result + outcome), `improvements` (history of improvement photos; the original photo is never overwritten).
- **Upload pipeline**: `/api/upload` → `analyze_image()` (resize to ≤1800px JPEG, OpenAI Responses API, JSON normalization) → `decide()` returns `matched` / `created` / `pending`. Without an API key, uploads go straight to `pending` — never fake AI results.
- **Matching safety rules in `decide()` are the core business logic** — keep them conservative: confidence < 0.8 or no board number → pending; an improvement needs an explicit 改善後 label and exactly one matching defect (by `full_id`, else `board_no`); a new defect needs a date and no existing same date+number. Manual resolution is `/api/review/<sid>/confirm`.
- **Statuses** (`VALID_STATUSES`): 待改善, 改善中, 已改善, 待複驗, 退回改善, 已結案. Auto-match sets only 已改善 (photo received); 已結案 is set manually after on-site re-inspection.
- **Export**: `/api/export.xlsx` builds the register with embedded original and improvement photos via XlsxWriter.

## Constraints

- Binds to `127.0.0.1` only, with no auth/HTTPS — a single-machine prototype. Don't expose it publicly or expand into production scope unless asked.
- Upload limits: JPG/PNG/WEBP, ≤20 files per batch, ≤12 MB each (`MAX_CONTENT_LENGTH` 65 MB per request).
