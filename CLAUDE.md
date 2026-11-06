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

No linter or build step. Regression checks for the matching rules (runs on a temp copy, fakes the AI, never touches `data/`; exits non-zero on failure):

```bash
.venv/bin/python tests/check_matching.py
```

Validate manually via the HTTP API:

```bash
curl http://127.0.0.1:5000/api/defects
curl -F photos=@photo.jpg http://127.0.0.1:5000/api/upload
curl -o out.xlsx http://127.0.0.1:5000/api/export.xlsx
```

Config (`.env`, never commit or print it): `OPENAI_API_KEY` (optional), `OPENAI_MODEL` (default `gpt-4.1-mini`), `PORT`.

## Architecture

Everything lives in `app.py` (backend) and `templates/index.html` (single-page frontend calling `/api/*`).

- **Storage**: SQLite at `data/defects.sqlite3`, photos in `data/images/`. `init_db()` runs at import time; if `defects` is empty it seeds 32 records from `seed.json` + `seed_images/`. Resetting means deleting `data/`, which holds user data — confirm first.
- **IDs**: each defect gets a global, never-reused `serial_no`, displayed as `RT-001` (`display_id()`); new defects need no whiteboard number. Legacy `public_id` (`MEP-20261104-021`) and `board_no` remain for old photos; improvement matching uses `RT-`/`DEF-` → serial, `MEP-` → public_id, and a bare number ("缺失 3") → `board_no` only. Never resolve a bare number as a serial: board numbers restart every inspection, so RT-003 almost always exists and would silently match the wrong defect.
- **Tables**: `defects` (soft-deleted via `deleted_at`), `submissions` (every uploaded photo + raw AI result + outcome + `photo_sha256` for duplicate detection), `improvements` (history; `voided_at` when a classification is reopened; original photo never overwritten), `audit_log` (before/after JSON of every change).
- **Migration**: `init_db()` upgrades a v1 database in place (`ALTER TABLE ADD COLUMN`, assigns serials by id) and first copies it to `data/backups/defects_before_v2_*.sqlite3`. Keep schema changes additive in the same way.
- **Admin** (`/api/admin/*`): edit fields, soft delete/restore, reopen a misclassified submission (voids the improvement and recomputes status via `refresh_improvement()`), recent audit log.
- **Upload pipeline** (`/api/upload`, 3 phases): validate + save originals sequentially → `analyze_image()` in a 4-thread pool (EXIF transpose, resize ≤1800px, OpenAI Responses API, JSON normalization) → DB writes sequentially in upload order, one `BEGIN IMMEDIATE` transaction per photo, so in-batch duplicate checks still work and one failure doesn't fail the batch. Without an API key, uploads go straight to `pending` — never fake AI results.
- **Matching safety rules in `decide()` are the core business logic** — keep them conservative. Pending when: confidence < 0.8; `full_id` number ≠ `board_no` (`id_conflict`); improvement without explicit 改善後 label, without a number, or not matching exactly one defect; target defect already 已結案; new defect without date or description, same date+board_no exists, or `find_similar_defect()` finds same date + floor with description similarity ≥ `SIMILAR_RATIO`; identical photo hash already uploaded or earlier in the same batch (checked before the AI call, so duplicates cost nothing). Manual resolution is `/api/review/<sid>/confirm`.
- **Statuses** (`VALID_STATUSES`): 待改善, 改善中, 已改善, 待複驗, 退回改善, 已結案. Auto-match sets only 已改善 (photo received); 已結案 is set manually after on-site re-inspection.
- **Export**: `/api/export.xlsx` builds the register with embedded original and improvement photos via XlsxWriter.

## Constraints

- Binds to `127.0.0.1` only, with no auth/HTTPS — a single-machine prototype. Don't expose it publicly or expand into production scope unless asked.
- Upload limits: JPG/PNG/WEBP, ≤20 files per batch, ≤12 MB each (`MAX_CONTENT_LENGTH` 65 MB per request).
