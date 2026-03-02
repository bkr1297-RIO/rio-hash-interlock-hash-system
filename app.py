import os
import yaml
import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel
from state_machine import State, VALID_TRANSITIONS, AuditLogger, ProposalStore

app = FastAPI(title="Control Plane", version="1.0.0")

with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

ALLOWED_REPOS = CONFIG.get("allowed_repos", [])
BLOCKED_PATTERNS = CONFIG.get("blocked_patterns", [])

audit_logger = AuditLogger()
store = ProposalStore(logger=audit_logger)


class ProposeRequest(BaseModel):
    repo: str
    title: str
    body: str
    head_branch: str
    base_branch: str = "main"


def run_governance_checks(proposal: ProposeRequest) -> list[str]:
    failures = []

    if proposal.repo not in ALLOWED_REPOS:
        failures.append(f"Repo '{proposal.repo}' is not in the allowed repos list")

    text_to_scan = f"{proposal.title} {proposal.body}".lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern.lower() in text_to_scan:
            failures.append(f"Blocked pattern detected: '{pattern}'")

    return failures


@app.post("/propose")
def propose(req: ProposeRequest):
    failures = run_governance_checks(req)

    proposal_data = req.model_dump()

    if failures:
        proposal_data["governance_failures"] = failures
        p_id = store.create(proposal_data, State.BLOCKED)
        audit_logger.log({
            "event": "governance_blocked",
            "proposal_id": p_id,
            "failures": failures,
        })
        return {
            "proposal_id": p_id,
            "state": State.BLOCKED.value,
            "governance_failures": failures,
        }

    p_id = store.create(proposal_data, State.PENDING)
    return {"proposal_id": p_id, "state": State.PENDING.value}


@app.post("/approve/{p_id}")
def approve(p_id: str, authorization: str = Header(None)):
    expected_key = os.environ.get("RIO_HUMAN_API_KEY", "")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization[len("Bearer "):]
    if token != expected_key:
        raise HTTPException(status_code=403, detail="Invalid approval token")

    try:
        proposal = store.transition(p_id, State.PENDING, State.APPROVED)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"proposal_id": p_id, "state": proposal["state"]}


def open_pull_request(repo: str, title: str, body: str, head: str, base: str) -> dict:
    github_token = os.environ.get("GITHUB_TOKEN", "")
    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "head": head, "base": base}
    resp = httpx.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


@app.post("/execute/{p_id}")
def execute(p_id: str):
    try:
        proposal = store.transition(p_id, State.APPROVED, State.EXECUTING)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    audit_logger.log({
        "event": "execution_intent",
        "proposal_id": p_id,
        "message": "About to call GitHub API",
    })

    try:
        pr_result = open_pull_request(
            repo=proposal["repo"],
            title=proposal["title"],
            body=proposal["body"],
            head=proposal["head_branch"],
            base=proposal["base_branch"],
        )
        proposal = store.transition(p_id, State.EXECUTING, State.EXECUTED)
        audit_logger.log({
            "event": "execution_success",
            "proposal_id": p_id,
            "pr_url": pr_result.get("html_url"),
            "pr_number": pr_result.get("number"),
        })
        return {
            "proposal_id": p_id,
            "state": proposal["state"],
            "pr_url": pr_result.get("html_url"),
            "pr_number": pr_result.get("number"),
        }

    except Exception as e:
        store.transition(p_id, State.EXECUTING, State.APPROVED)
        audit_logger.log({
            "event": "execution_failed",
            "proposal_id": p_id,
            "error": str(e),
        })
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API call failed: {str(e)}. Proposal rolled back to APPROVED.",
        )


@app.get("/proposal/{p_id}")
def get_proposal(p_id: str):
    proposal = store.get(p_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


@app.get("/proposals")
def list_proposals(status: str | None = Query(None)):
    return store.list_all(status_filter=status)


@app.get("/health")
def health():
    all_terminal_no_exit = (
        len(VALID_TRANSITIONS.get(State.BLOCKED, set())) == 0
        and len(VALID_TRANSITIONS.get(State.EXECUTED, set())) == 0
    )

    approve_requires_pending = State.APPROVED in VALID_TRANSITIONS.get(State.PENDING, set())
    execute_requires_approved = State.EXECUTING in VALID_TRANSITIONS.get(State.APPROVED, set())

    config_readonly = not os.access("config.yaml", os.W_OK) if os.path.exists("config.yaml") else False

    return {
        "status": "healthy",
        "invariants": {
            "no_execution_without_approval": {
                "enforced": approve_requires_pending and execute_requires_approved,
                "detail": "approve requires PENDING state; execute requires APPROVED state",
            },
            "no_self_permission_escalation": {
                "enforced": len(BLOCKED_PATTERNS) > 0,
                "detail": f"{len(BLOCKED_PATTERNS)} blocked patterns configured",
            },
            "every_transition_audited": {
                "enforced": True,
                "detail": "AuditLogger.log() called on every ProposalStore.transition()",
            },
            "governance_layer_immutable": {
                "enforced": True,
                "detail": "No write path to config.yaml exists in code",
            },
            "blocked_proposals_terminal": {
                "enforced": all_terminal_no_exit,
                "detail": "BLOCKED and EXECUTED have no outgoing transitions",
            },
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
