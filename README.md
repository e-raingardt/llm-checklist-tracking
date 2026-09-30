# LLM Workflow for Checklist Tracking

A local demo for tracking document checklists from emails and attachment filenames with Claude.

https://github.com/user-attachments/assets/1588a2c2-37f4-4be4-a76e-85913fa36297

## Overview

Inspired by a repetitive workflow from my previous job: manually checking emails to determine which requested documents had arrived. The app turns that process into an auditable pipeline with a React UI, FastAPI backend, and SQLite storage.

Claude proposes status updates; deterministic rules validate them before they are applied. Each checklist item moves through `open`, `requested`, `expected`, and `received`.

## Current State

The interactive prototype includes:

- `.eml` parsing and attachment filename extraction
- LLM-based document matching
- validation against unknown items, invalid states, and status regressions
- persistent checklist, email, proposal, and event history in SQLite
- FastAPI backend and React frontend
- fictional email fixtures and unit tests

Automated email ingestion and parsing the contents of PDF or Word attachments are not implemented.

## Run Locally

Create a `.env` file containing `ANTHROPIC_API_KEY`, then:

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Run the tests with `pytest`.

## Project Structure

```text
├── api.py                 # FastAPI endpoints
├── db.py                  # SQLite schema and access
├── pipeline.py            # LLM analysis and validation logic
├── seed.py                # Demo reset and fixture replay
├── fixtures/              # Fictional sample emails
├── frontend/              # React/Vite interface
├── test_checklist.py      # State-transition tests
└── requirements.txt
```

## Data Privacy

This is a local demo without authentication. Email text is sent to the Anthropic API and stored locally in SQLite. Do not use or commit real personal or confidential data.
