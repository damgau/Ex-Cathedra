# WAT Framework Template

The **WAT framework** (Workflows, Agents, Tools) is a lightweight architecture for reliable AI-assisted automation. It keeps probabilistic AI (reasoning, orchestration) separate from deterministic code (execution), so accuracy compounds instead of decaying. Workflows define *what* to do in plain Markdown SOPs; Tools are Python scripts that *do* the work; the Agent (Claude) reads the workflow, sequences the tools, and improves the system when things go wrong.

## Starting a new project from this template

```bash
# 1. Copy the template folder
cp -r "Ex Cathedra03" my-new-project
cd my-new-project

# 2. Set up environment variables
cp .env.example .env
# Edit .env and fill in real API keys

# 3. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

## Where things live

| Path | Purpose |
|------|---------|
| `workflows/` | Markdown SOPs — read these to understand how a task works |
| `tools/` | Python scripts — the deterministic execution layer |
| `.env` | API keys and secrets (never committed) |
| `.tmp/` | Intermediate/temporary files (disposable, auto-generated) |

Start by copying `workflows/_TEMPLATE.md` for each new task and `tools/_template.py` for each new script.
