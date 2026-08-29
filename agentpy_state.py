"""JSON persistence shared by dataclass-based agents."""

from dataclasses import asdict, fields
import json
from pathlib import Path


class AgentState:
    """Persist every dataclass field in a per-agent JSON file."""

    def _state_path(self) -> Path:
        return self.workdir / ".agentpy" / f"{self.agid}.json"

    def save(self) -> None:
        """Write this agent's state to .agentpy/<agid>.json."""
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = asdict(self)
        state["workdir"] = str(self.workdir)

        temporary_path = state_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(state, indent=2) + "\n")
        temporary_path.replace(state_path)

    def load(self) -> None:
        """Restore this agent's state from .agentpy/<agid>.json."""
        state_path = self._state_path()
        if not state_path.exists():
            raise FileNotFoundError(f"No saved state found for {self.agid}.")

        state = json.loads(state_path.read_text())
        if state.get("agid") != self.agid:
            raise ValueError("Saved state belongs to a different agent.")

        for state_field in fields(self):
            value = state[state_field.name]
            if state_field.name == "workdir":
                value = Path(value)
            setattr(self, state_field.name, value)
