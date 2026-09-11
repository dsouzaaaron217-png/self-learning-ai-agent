# Cognito: Offline Self-Learning Personal Productivity Agent

> **Positioning**: A 100% on-device, self-learning personal productivity agent that runs entirely on your local machine with zero external API calls or cloud dependencies. Cognito remembers your habits, notes, tasks, and corrections over time.

---

## Key Features

1. **100% Offline Operation & Zero-Cloud Security**:
   - Built-in network firewall restricting all non-localhost connections.
   - All data is stored locally in SQLite (`data/cognito_agent.db`) with an on-device vector index (`data/vector_store.json`).
   - Zero telemetry, zero analytics tracking, zero third-party scripts or CDNs.

2. **Dual Reasoning Architecture**:
   - **Engine A (Built-in Local Reasoner)**: A pure-Python deterministic & semantic reasoning engine that runs out of the box with zero setup or GPU requirements.
   - **Engine B (Pluggable Local LLM Adapter)**: Seamlessly connects to any locally running Ollama instance (`http://127.0.0.1:11434`) or LM Studio instance. Automatically falls back to the built-in reasoner if the local daemon is stopped.

3. **Two-Phase Memory Pipeline (PRD §5.4)**:
   - **Phase 1 (Extraction)**: Extracts discrete atomic habits, preferences, and facts from captures, notes, and task corrections.
   - **Phase 2 (Reconciliation)**: Compares extracted facts with existing memories to decide:
     - `ADD`: Stores novel preferences with base confidence.
     - `UPDATE`: Refines or updates existing memories, increments confidence weights, and records change provenance.
     - `DELETE`: Invalidates outdated habits when negated by the user.

4. **Feedback & Continuous Reinforcement Loop**:
   - Every AI task recommendation can be **Accepted** ("Looks Good" → strengthens confidence +0.15) or **Corrected** ("Teach & Correct" → updates the memory store).
   - Tracks adaptation metrics: correction rate over time declining as the agent learns your habits.

5. **Transparency Dashboard (PRD §5.5)**:
   - Complete visibility into what Cognito has learned about you.
   - Category filtering: `Habits`, `Preferences`, `Corrections`, `Facts`.
   - **"Why this exists" Provenance Drawer**: View the full evolutionary history and decision trail for any memory.
   - Live **Decision Audit Log** inspecting all ADD, UPDATE, and DELETE actions.
   - Full CRUD control to edit, adjust confidence weights, or delete memories.

6. **Productivity Workspace**:
   - **Quick Capture Omnibar (`Ctrl + K`)**: Instantly capture tasks and notes with automatic hashtag parsing (`#tag`) and smart priority detection.
   - **Kanban & List Tasks Board**: Interactive status columns (`To Do`, `In Progress`, `Completed`) with AI subtask breakdown checklists.
   - **Notes & Knowledge Base**: Markdown editor with instant semantic search and auto-fact discovery.
   - **AI Copilot Chat**: Offline assistant grounded in your local tasks, notes, and learned habits with clickable citation tags.
   - **Encrypted Data Vault**: 1-click JSON backup export and restore.

---

## Quick Start

### Prerequisites
- Python 3.10+ (tested on Python 3.13)
- `Flask` (`pip install flask`)

### Running the Application

```bash
# Start the local agent on port 5000
python run.py

# Or specify a custom port / host
python run.py --port 8080 --host 127.0.0.1
```

Open your browser at:
```
http://127.0.0.1:5000
```

### Running Automated Tests

Run the full unit and integration test suite:

```bash
python -m unittest discover tests
```

---

## Project Structure

```
d:\Self_learning AI-agent\
├── app/
│   ├── __init__.py               # Flask application factory & database seeder
│   ├── config.py                 # Local storage paths, thresholds, and settings
│   ├── database.py               # SQLite schema, WAL connection manager
│   ├── models.py                 # Task, Note, Memory, DecisionLog, Feedback models
│   ├── vector_store.py           # Pure-Python TF-IDF & Cosine Similarity vector store
│   ├── reasoning/
│   │   ├── base.py               # Abstract reasoning engine interface
│   │   ├── local_reasoner.py     # 100% offline zero-dependency semantic reasoner
│   │   ├── ollama_adapter.py     # Local Ollama connector with graceful fallback
│   │   └── __init__.py           # Engine selector factory
│   ├── memory/
│   │   ├── pipeline.py           # Two-phase memory pipeline (Extract -> Add/Update/Delete)
│   │   ├── tracker.py            # Adaptation metrics & correction trend calculations
│   │   └── __init__.py
│   ├── routes/
│   │   ├── api_tasks.py          # Task CRUD, AI suggestions, and feedback endpoints
│   │   ├── api_notes.py          # Notes CRUD, auto-fact extraction, semantic search
│   │   ├── api_memory.py         # Transparency dashboard, provenance, audit logs
│   │   ├── api_chat.py           # Offline copilot chat with memory citations
│   │   └── api_backup.py         # Encrypted JSON backup export & restore
│   ├── static/
│   │   ├── css/style.css         # Modern dark/light theme (zero external CDN)
│   │   └── js/
│   │       ├── app.js            # App lifecycle, shortcuts (Ctrl+K), modals
│   │       ├── tasks.js          # Kanban board & feedback loop
│   │       ├── notes.js          # Notes grid & semantic search
│   │       ├── memory.js         # Transparency dashboard & provenance viewer
│   │       ├── chat.js           # Offline copilot chat interface
│   │       └── settings.js       # Engine toggles & data vault
│   └── templates/
│       └── index.html            # Main single-page application shell
├── data/                         # Local SQLite DB and vector index (persisted)
├── tests/                        # Comprehensive automated test suite
├── run.py                        # Entrypoint with strict offline firewall
└── README.md
```

---

## Verifying Zero Network Calls

The application includes an active security guard in `run.py` that intercepts all outbound socket lookups (`socket.getaddrinfo`). Any attempt to connect to an external server or domain outside `localhost` / `127.0.0.1` immediately raises a `ConnectionRefusedError`.

You can also run Wireshark or Windows Resource Monitor on the `python.exe` process to verify that zero external WAN packets are transmitted during any operation.
