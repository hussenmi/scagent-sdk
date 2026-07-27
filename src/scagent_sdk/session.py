"""High-level scientific session API."""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

from scagent_sdk.contracts.events import utc_now
from scagent_sdk.output_view import refresh_output_view
from scagent_sdk.runtime.resume import ResumePlan, ResumePreference, plan_resume
from scagent_sdk.state.store import SessionStore


class AnalysisSession:
    """A durable scientific session independent of any one model conversation."""

    def __init__(self, store: SessionStore):
        self.store = store

    @property
    def session_id(self) -> str:
        return self.store.session_id

    @property
    def directory(self) -> Path:
        return self.store.session_dir

    @classmethod
    def create(
        cls,
        sessions_root: str | Path,
        *,
        title: str,
        session_id: str | None = None,
    ) -> AnalysisSession:
        session = cls(SessionStore.create(sessions_root, title=title, session_id=session_id))
        session.refresh_outputs_best_effort()
        return session

    @classmethod
    def resume(cls, sessions_root: str | Path, session_id: str) -> AnalysisSession:
        session = cls(SessionStore.open(sessions_root, session_id))
        session.refresh_outputs_best_effort()
        return session

    def refresh_outputs(self) -> dict[str, Any]:
        commit_sequences = {
            str(event.payload["execution_id"]): event.sequence
            for event in self.store.events()
            if event.kind == "capability.result_committed"
            and isinstance(event.payload.get("execution_id"), str)
        }
        return refresh_output_view(
            self.directory,
            self.store.state.artifacts,
            commit_sequences=commit_sequences,
        )

    def refresh_outputs_best_effort(self) -> dict[str, Any] | None:
        """Refresh the disposable view without jeopardizing authoritative session state."""

        try:
            return self.refresh_outputs()
        except OSError as exc:
            with suppress(OSError):
                self.store.record(
                    "session.output_view_refresh_failed",
                    payload={"error_type": type(exc).__name__, "error": str(exc)},
                )
            return None

    def checkpoint_facts(
        self,
        facts: dict[str, Any],
        *,
        reason: str,
        actor: str = "harness",
    ) -> None:
        self.store.record(
            "state.facts_checkpointed",
            payload={"reason": reason},
            state_patch={"facts": facts},
            actor=actor,
        )

    def bind_runtime(
        self,
        *,
        runtime: str,
        runtime_session_id: str,
        model_profile: str,
        model_profile_fingerprint: str | None = None,
        transport: str,
        model: str | None = None,
    ) -> None:
        current_history = self.store.state.runtime.get("history", [])
        history = list(current_history) if isinstance(current_history, list) else []
        binding = {
            "runtime": runtime,
            "runtime_session_id": runtime_session_id,
            "model_profile": model_profile,
            "model_profile_fingerprint": model_profile_fingerprint,
            "transport": transport,
            "model": model,
            "bound_at": utc_now(),
        }
        history.append(binding)
        self.store.record(
            "runtime.bound",
            payload=binding,
            state_patch={"runtime": {"active": binding, "history": history}},
        )

    def plan_resume(
        self,
        *,
        runtime: str,
        model_profile: str,
        model_profile_fingerprint: str | None = None,
        preference: ResumePreference = ResumePreference.AUTO,
    ) -> ResumePlan:
        return plan_resume(
            self.store.metadata,
            self.store.state,
            runtime=runtime,
            model_profile=model_profile,
            model_profile_fingerprint=model_profile_fingerprint,
            preference=preference,
            events=self.store.events(),
            session_dir=self.directory,
        )

    def fork(
        self,
        *,
        title: str,
        session_id: str | None = None,
    ) -> AnalysisSession:
        forked = AnalysisSession(
            SessionStore.create(
                self.directory.parent,
                title=title,
                session_id=session_id,
                parent_session_id=self.session_id,
            )
        )
        active_runtime = self.store.state.runtime.get("active")
        forked.store.record(
            "session.forked",
            payload={
                "parent_session_id": self.session_id,
                "parent_revision": self.store.state.revision,
                "parent_last_event_sequence": self.store.state.last_event_sequence,
            },
            state_patch={
                "facts": deepcopy(self.store.state.facts),
                "artifacts": deepcopy(self.store.state.artifacts),
                "decisions": deepcopy(self.store.state.decisions),
                "runtime": {
                    "active": None,
                    "history": [],
                    "fork_origin": deepcopy(active_runtime),
                },
            },
        )
        forked.refresh_outputs_best_effort()
        return forked

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.store.metadata.to_dict(),
            "state": self.store.state.to_dict(),
            "directory": str(self.directory),
        }
