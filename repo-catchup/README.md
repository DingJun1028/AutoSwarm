# repo-catchup

A Mistral Workflow that catches you up on a GitHub repo:

- **Input**: `repo` (`owner/name` or a GitHub URL) and `since` (`YYYY-MM-DD`).
- **Merged**: pull requests merged since that date (`is:merged merged:>=<since>`).
- **Waiting on review**: open, non-draft pull requests.
- Each PR gets a one-line summary (generated with `mistral-medium-latest`) and a link.
- The digest is posted to the triggering user's Slack self-chat (DM with yourself).

The workflow is on-behalf-of (`on_behalf_of=True`): GitHub reads and the Slack post
run with the triggering user's connector credentials, so the deployment must be
**hardened**. On first run, if no Slack credential is stored for the triggering
user yet, the execution pauses with an OAuth `auth_url` — complete it once and the
workflow resumes (or pre-add a credential in Studio → Context → Connectors → slack).

## Layout

- `src/worker/workflows/repo_catchup.py` — the `repo-catchup` workflow and its activities.
- `src/worker/entrypoints/worker.py` — worker entrypoint; auto-discovers workflows in
  `worker.workflows` and calls `run_worker`.
- `Dockerfile` — builds the image Mistral Cloud runs for the managed deployment.
  The platform injects `DEPLOYMENT_NAME`, `SERVER_URL` and `MISTRAL_SA_TOKEN_PATH`;
  the image only has to start the worker.

## Local dev

```bash
uv venv && source .venv/bin/activate
uv pip install "mistralai-workflows[mistralai]>=3.10"
export MISTRAL_API_KEY=...   # local only; never commit it (.env is git-ignored)
PYTHONPATH=src python -m worker.entrypoints.worker
```

## Deploy (managed)

Deployment `repo-catchup`, built from this repository at `revision` =
`vibe/repo-catchup-9cc06a`, `build_directory` = `repo-catchup`,
`dockerfile_path` = `Dockerfile`, created **hardened** (required for
on-behalf-of workflows):

```python
from mistralai.client import Mistral

client = Mistral()
client.workflows.deployments.create_deployment(
    name="repo-catchup",
    spec={
        "github_url": "https://github.com/DingJun1028/AutoSwarm",
        "revision": "vibe/repo-catchup-9cc06a",
        "backend_spec": {
            "type": "mistral_cloud",
            "build_directory": "repo-catchup",
            "dockerfile_path": "Dockerfile",
        },
    },
)
```

## Run

```python
client.workflows.execute_workflow(
    workflow_identifier="repo-catchup",
    input={"repo": "owner/name", "since": "2026-11-01"},
)
```

or from Studio → Workflows → `repo-catchup` → Start workflow.
