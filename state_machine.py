import enum
import json
import os
import threading
import tempfile
import uuid
from datetime import datetime, timezone


class State(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    BLOCKED = "BLOCKED"
    EXECUTED = "EXECUTED"


VALID_TRANSITIONS = {
    State.PENDING: {State.APPROVED, State.BLOCKED},
    State.APPROVED: {State.EXECUTING},
    State.EXECUTING: {State.EXECUTED, State.APPROVED},
    State.BLOCKED: set(),
    State.EXECUTED: set(),
}


class AuditLogger:
    def __init__(self, path="audit.jsonl"):
        self._path = path
        self._lock = threading.Lock()

    def log(self, entry: dict):
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)
            finally:
                os.close(fd)


class ProposalStore:
    def __init__(self, path="proposals.json", logger: AuditLogger | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._logger = logger or AuditLogger()
        if not os.path.exists(self._path):
            self._write({})

    def _read(self) -> dict:
        with open(self._path, "r") as f:
            return json.load(f)

    def _write(self, data: dict):
        dir_name = os.path.dirname(self._path) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            os.write(fd, json.dumps(data, indent=2, default=str).encode())
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp, self._path)
        except Exception:
            os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def create(self, proposal: dict, initial_state: State) -> str:
        p_id = str(uuid.uuid4())
        proposal["id"] = p_id
        proposal["state"] = initial_state.value
        proposal["created_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            data = self._read()
            data[p_id] = proposal
            self._write(data)
        self._logger.log({
            "event": "proposal_created",
            "proposal_id": p_id,
            "initial_state": initial_state.value,
            "proposal": proposal,
        })
        return p_id

    def get(self, p_id: str) -> dict | None:
        data = self._read()
        return data.get(p_id)

    def list_all(self, status_filter: str | None = None) -> list[dict]:
        data = self._read()
        proposals = list(data.values())
        if status_filter:
            proposals = [p for p in proposals if p.get("state") == status_filter]
        return proposals

    def transition(self, p_id: str, expected_current: State, new_state: State) -> dict:
        with self._lock:
            data = self._read()
            proposal = data.get(p_id)
            if proposal is None:
                raise ValueError(f"Proposal {p_id} not found")

            actual_current = State(proposal["state"])

            if actual_current != expected_current:
                raise ValueError(
                    f"State mismatch: expected {expected_current.value}, "
                    f"actual {actual_current.value}"
                )

            if new_state not in VALID_TRANSITIONS.get(actual_current, set()):
                raise ValueError(
                    f"Invalid transition: {actual_current.value} -> {new_state.value}"
                )

            proposal["state"] = new_state.value
            proposal["updated_at"] = datetime.now(timezone.utc).isoformat()
            data[p_id] = proposal
            self._write(data)

        self._logger.log({
            "event": "state_transition",
            "proposal_id": p_id,
            "from_state": expected_current.value,
            "to_state": new_state.value,
        })

        return proposal
