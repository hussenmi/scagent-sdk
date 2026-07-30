"""Command-line interface for scientific-session lifecycle operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from scagent_sdk.capabilities.assembly import CapabilityAssembler
from scagent_sdk.capabilities.readiness import probe_packages
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.doctor.agent import AVAILABLE_PROBES, AgentCompatibilityDoctor
from scagent_sdk.doctor.model import GatewayDoctor
from scagent_sdk.errors import ScagentSDKError
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.models.limits import ModelLimitResolver
from scagent_sdk.models.profile import ModelProfileRegistry
from scagent_sdk.runtime.claude import ClaudeAgentSDKBackend
from scagent_sdk.runtime.interactive import InteractiveAgent
from scagent_sdk.runtime.interrupts import TurnInterrupter
from scagent_sdk.runtime.resume import ResumePreference
from scagent_sdk.runtime.service import AgentRuntimeService
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.retention import propose_prune
from scagent_sdk.state.store import SessionStore


def _default_sessions_root() -> Path:
    configured = os.environ.get("SCAGENT_SDK_SESSIONS_DIR")
    return Path(configured).expanduser() if configured else Path.cwd() / "sessions"


def _load_project_environment() -> Path | None:
    configured = os.environ.get("SCAGENT_SDK_ENV_FILE")
    if configured:
        candidate = Path(configured).expanduser().resolve()
    else:
        project_root = Path(
            os.environ.get("SCAGENT_SDK_PROJECT_ROOT", Path(__file__).resolve().parents[2])
        ).expanduser()
        candidate = (project_root / ".env").resolve()
    if not candidate.is_file():
        return None
    load_dotenv(candidate, override=False)
    return candidate


def _default_start_profile() -> str:
    return os.environ.get("SCAGENT_SDK_MODEL_PROFILE", "iris-qwen36")


def _default_profiles_root() -> Path:
    configured = os.environ.get("SCAGENT_SDK_MODEL_PROFILES_DIR")
    if configured:
        return Path(configured).expanduser()
    project_profiles = Path(__file__).resolve().parents[2] / "configs" / "models"
    if project_profiles.is_dir():
        return project_profiles
    return Path(str(files("scagent_sdk") / "configs" / "models"))


def _default_skills_root() -> Path:
    configured = os.environ.get("SCAGENT_SDK_SKILLS_DIR")
    if configured:
        return Path(configured).expanduser()
    working_skills = Path.cwd() / ".claude" / "skills"
    if working_skills.is_dir():
        return working_skills
    project_skills = Path(__file__).resolve().parents[2] / ".claude" / "skills"
    if project_skills.is_dir():
        return project_skills
    return Path(str(files("scagent_sdk") / "skills"))


def _default_environments_file() -> Path | None:
    configured = os.environ.get("SCAGENT_SDK_ENVIRONMENTS_FILE")
    if configured:
        return Path(configured).expanduser()
    candidate = Path(__file__).resolve().parents[2] / "configs" / "environments" / "iris.toml"
    return candidate if candidate.is_file() else None


def _sessions_root(args: argparse.Namespace) -> Path:
    value = getattr(args, "sessions_root", None)
    return Path(value).expanduser() if value else _default_sessions_root()


def _skills_root(args: argparse.Namespace) -> Path:
    value = getattr(args, "skills_root", None)
    return Path(value).expanduser() if value else _default_skills_root()


def _environment_broker(args: argparse.Namespace) -> EnvironmentBroker | None:
    value = getattr(args, "environments_file", None)
    path = Path(value).expanduser() if value else _default_environments_file()
    return EnvironmentBroker(EnvironmentRegistry.from_path(path)) if path else None


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _cmd_session_new(args: argparse.Namespace) -> int:
    session = AnalysisSession.create(_sessions_root(args), title=args.title)
    _print_json(session.summary())
    return 0


def _cmd_session_list(args: argparse.Namespace) -> int:
    items = [item.to_dict() for item in SessionStore.list_sessions(_sessions_root(args))]
    _print_json(items)
    return 0


def _cmd_session_show(args: argparse.Namespace) -> int:
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    value = session.summary()
    value["events"] = [event.to_dict() for event in session.store.events()]
    _print_json(value)
    return 0


def _cmd_session_storage(args: argparse.Namespace) -> int:
    """Report artifact storage and what an unreachable-version prune would actually reclaim.

    Read-only by construction: there is no deletion path here. ``reclaimable_bytes`` is the only
    figure a future prune may promise, because apparent size and reclaimable size diverge once
    versions can share bytes.
    """

    roots = (
        [args.session_id]
        if args.session_id
        else [item.session_id for item in SessionStore.list_sessions(_sessions_root(args))]
    )
    reports = []
    for session_id in roots:
        session = AnalysisSession.resume(_sessions_root(args), session_id)
        reports.append(
            propose_prune(
                session.directory,
                session_id=session_id,
                lineage=session.store.state.lineage,
                artifacts=session.store.state.artifacts,
            ).to_dict()
        )
    if args.session_id:
        _print_json(reports[0])
        return 0
    _print_json(
        {
            "sessions": reports,
            "totals": {
                key: sum(report["session_total"][key] for report in reports)
                for key in ("apparent_bytes", "unique_bytes", "reclaimable_bytes", "files")
            },
            "candidate_totals": {
                key: sum(report["candidate_total"][key] for report in reports)
                for key in ("apparent_bytes", "unique_bytes", "shared_bytes", "reclaimable_bytes")
            },
        }
    )
    return 0


def _cmd_session_resume(args: argparse.Namespace) -> int:
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    profile = _profiles(args).load(args.model_profile)
    plan = session.plan_resume(
        runtime=args.runtime,
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
    )
    _print_json(
        {
            "mode": plan.mode.value,
            "scientific_session_id": plan.scientific_session_id,
            "runtime_session_id": plan.runtime_session_id,
            "reason": plan.reason,
            "context": plan.context,
        }
    )
    return 0


def _cmd_session_bind_runtime(args: argparse.Namespace) -> int:
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    fingerprint = args.model_profile_fingerprint
    if fingerprint is None:
        fingerprint = _profiles(args).load(args.model_profile).fingerprint
    session.bind_runtime(
        runtime=args.runtime,
        runtime_session_id=args.runtime_session_id,
        model_profile=args.model_profile,
        model_profile_fingerprint=fingerprint,
        transport=args.transport,
        model=args.model,
    )
    _print_json(session.summary())
    return 0


def _cmd_session_fork(args: argparse.Namespace) -> int:
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    forked = session.fork(title=args.title, session_id=args.new_session_id)
    _print_json(forked.summary())
    return 0


def _profiles(args: argparse.Namespace) -> ModelProfileRegistry:
    value = getattr(args, "profiles_root", None)
    return ModelProfileRegistry(value or _default_profiles_root())


def _cmd_model_list(args: argparse.Namespace) -> int:
    profiles = [
        profile.to_dict() | {"fingerprint": profile.fingerprint}
        for profile in _profiles(args).list()
    ]
    _print_json(profiles)
    return 0


def _cmd_model_show(args: argparse.Namespace) -> int:
    profile = _profiles(args).load(args.profile)
    _print_json(profile.to_dict() | {"fingerprint": profile.fingerprint})
    return 0


def _cmd_capability_list(args: argparse.Namespace) -> int:
    packages = CapabilityRegistry(_skills_root(args)).discover()
    _print_json(
        [
            package.manifest.to_dict()
            | {
                "root": str(package.root),
                "instructions_path": str(package.instructions_path),
                "fingerprint": package.fingerprint,
            }
            for package in packages
        ]
    )
    return 0


def _cmd_capability_validate(args: argparse.Namespace) -> int:
    registry = CapabilityRegistry(_skills_root(args))
    packages = registry.discover()
    skills = registry.skills()
    readiness = probe_packages(packages, broker=_environment_broker(args))
    _print_json(
        {
            "status": "pass",
            "skills_root": str(_skills_root(args).expanduser().resolve()),
            "skills": len(skills),
            "executable_skills": len(packages),
            "tools": sum(len(package.manifest.tools) for package in packages),
            "readiness": [report.to_dict() for report in readiness],
        }
    )
    return 0


def _cmd_doctor_model(args: argparse.Namespace) -> int:
    profile = _profiles(args).load(args.profile)
    doctor = GatewayDoctor(profile, timeout=args.timeout)
    if args.no_network:
        results = [doctor.profile_check()]
    else:
        results = doctor.run(probe_messages=args.probe_messages)
    _print_json([result.to_dict() for result in results])
    return 0 if all(result.status in {"pass", "skip"} for result in results) else 1


def _cmd_doctor_agent(args: argparse.Namespace) -> int:
    profile = _profiles(args).load(args.profile)
    doctor = AgentCompatibilityDoctor(
        profile,
        sessions_root=_sessions_root(args),
        cwd=args.cwd or Path.cwd(),
        long_result_chars=args.long_result_chars,
    )
    results = asyncio.run(doctor.run(args.checks))
    _print_json([result.to_dict() for result in results])
    return 0 if all(result.status in {"pass", "warn", "skip"} for result in results) else 1


def _cmd_doctor_environment(args: argparse.Namespace) -> int:
    broker = _environment_broker(args)
    if broker is None:
        raise ScagentSDKError("no environment profile file is configured")
    _print_json(broker.probe_all())
    return 0


def _cmd_agent_run(args: argparse.Namespace) -> int:
    profile = _profiles(args).load(args.profile)
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    extensions = CapabilityAssembler(
        CapabilityRegistry(_skills_root(args)),
        session,
        environment_broker=_environment_broker(args),
    ).assemble()
    service = AgentRuntimeService(
        ClaudeAgentSDKBackend(extensions=extensions),
        model_limits=ModelLimitResolver(profile).resolve(),
    )
    response = asyncio.run(
        service.run_turn(
            session,
            user_prompt=args.prompt,
            profile=profile,
            cwd=args.cwd or Path(__file__).resolve().parents[2],
        )
    )
    _print_json(response.to_dict())
    return 0


def _cmd_agent_chat(args: argparse.Namespace) -> int:
    profile = _profiles(args).load(args.profile)
    session = AnalysisSession.resume(_sessions_root(args), args.session_id)
    broker = _environment_broker(args)
    extensions = CapabilityAssembler(
        CapabilityRegistry(_skills_root(args)),
        session,
        environment_broker=broker,
    ).assemble()
    backend = ClaudeAgentSDKBackend(extensions=extensions)
    agent = InteractiveAgent(
        service=AgentRuntimeService(
            backend,
            model_limits=ModelLimitResolver(profile).resolve(),
        ),
        session=session,
        profile=profile,
        cwd=Path(args.cwd or Path.cwd()).expanduser().resolve(),
        interrupter=TurnInterrupter(backend, broker=broker),
    )
    try:
        return asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nSession ended. State is preserved.")
        return 0


def _gateway_config(profile: Any, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if profile.source_path is None:
        return None
    candidate = profile.source_path.parents[1] / "litellm" / f"{profile.name}.yaml"
    return candidate if candidate.is_file() else None


def _resolve_thinking(profile: Any, args: argparse.Namespace) -> Any:
    """Layer reasoning config: profile defaults < environment < CLI flags."""

    from scagent_sdk.models.thinking import apply_env, apply_overrides

    settings = apply_env(profile.thinking, os.environ)
    mode = {"on": "enabled", "off": "disabled"}.get(args.thinking, args.thinking)
    return apply_overrides(
        settings,
        mode=mode,
        budget_tokens=args.thinking_budget,
        effort=args.thinking_effort,
        show=args.show_thinking,
        save=args.save_thinking,
    )


def _start_session(args: argparse.Namespace) -> tuple[AnalysisSession, bool]:
    sessions_root = _sessions_root(args)
    if args.resume is not None:
        session_id = args.resume
        if session_id == "latest":
            sessions = SessionStore.list_sessions(sessions_root)
            if not sessions:
                raise ScagentSDKError("there is no previous session to resume")
            session_id = sessions[0].session_id
        return AnalysisSession.resume(sessions_root, session_id), True
    return AnalysisSession.create(sessions_root, title=args.title), False


def _start_profile_name(
    args: argparse.Namespace,
    session: AnalysisSession,
    *,
    resumed: bool,
) -> str:
    """Prefer the recorded profile on resume unless the user explicitly selects another."""

    if args.profile:
        return str(args.profile)
    if resumed:
        active = session.store.state.runtime.get("active")
        recorded = active.get("model_profile") if isinstance(active, dict) else None
        if isinstance(recorded, str) and _profiles(args).path_for(recorded).is_file():
            return recorded
    return _default_start_profile()


def _cmd_start(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        print("Interactive session requires a TTY. Run scagent-sdk start from a terminal.")
        return 1
    from rich.console import Console

    from scagent_sdk.runtime.gateway import GatewaySupervisor
    from scagent_sdk.terminal.app import RichInteractiveAgent
    from scagent_sdk.terminal.rendering import RichRuntimeObserver
    from scagent_sdk.terminal.resume import choose_resume_preference

    console = Console()
    session, resumed = _start_session(args)
    profile = _profiles(args).load(_start_profile_name(args, session, resumed=resumed))
    profile = replace(profile, thinking=_resolve_thinking(profile, args))
    resume_preference = ResumePreference.AUTO
    if resumed:
        natural_plan = session.plan_resume(
            runtime="claude-agent-sdk",
            model_profile=profile.name,
            model_profile_fingerprint=profile.fingerprint,
        )
        resume_preference = choose_resume_preference(console, natural_plan)
    registry = CapabilityRegistry(_skills_root(args))
    packages = registry.discover()
    discovered_skills = registry.skills()
    package_by_id = {package.manifest.skill_id: package for package in packages}
    broker = _environment_broker(args)
    gateway = GatewaySupervisor(
        profile,
        config_path=_gateway_config(profile, args.gateway_config),
        log_path=session.directory / "logs" / "litellm.log",
        auto_start=not args.no_auto_gateway,
    )
    try:
        with console.status("Checking model gateway...", spinner="dots"):
            gateway.ensure()
        with console.status("Discovering model context window...", spinner="dots"):
            model_limits = ModelLimitResolver(profile).resolve()
        reasoning_log = (
            session.directory / "logs" / "reasoning.log" if profile.thinking.save else None
        )
        observer = RichRuntimeObserver(
            console,
            show_thinking=profile.thinking.show,
            reasoning_log=reasoning_log,
        )
        extensions = CapabilityAssembler(
            CapabilityRegistry(_skills_root(args)),
            session,
            observer=observer,
            environment_broker=broker,
        ).assemble()
        backend = ClaudeAgentSDKBackend(extensions=extensions, observer=observer)
        app = RichInteractiveAgent(
            service=AgentRuntimeService(
                backend,
                model_limits=model_limits,
                resume_preference=resume_preference,
                observer=observer,
            ),
            session=session,
            profile=profile,
            cwd=Path(args.cwd or Path.cwd()).expanduser().resolve(),
            console=console,
            interrupter=TurnInterrupter(backend, broker=broker),
            model_limits=model_limits,
            resume_preference=resume_preference if resumed else None,
            skills=tuple(
                {
                    "id": skill.name,
                    "fingerprint": skill.fingerprint,
                    "executable": skill.executable,
                    "tools": [tool.name for tool in package_by_id[skill.name].manifest.tools]
                    if skill.name in package_by_id
                    else [],
                }
                for skill in discovered_skills
            ),
        )
        app.welcome(gateway_managed=gateway.managed)
        initial_prompt = args.prompt
        if args.data:
            initial_prompt = (
                "Use the appropriate inspection skill to inspect this dataset and record its "
                f"identity before analysis: {Path(args.data).expanduser().resolve()}"
                + (f"\n\nThen address this request: {args.prompt}" if args.prompt else "")
            )
        try:
            return asyncio.run(app.run(initial_prompt=initial_prompt))
        except KeyboardInterrupt:
            # Last-resort net for a Ctrl+C landing outside a turn's own interrupt handling:
            # exit the way /exit does rather than showing a traceback.
            console.print("\n[dim]Session ended. State is preserved.[/dim]")
            return 0
    finally:
        gateway.stop()


def _add_sessions_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-root",
        help="session storage directory (default: $SCAGENT_SDK_SESSIONS_DIR or ./sessions)",
    )


def _add_profiles_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profiles-root",
        help="model-profile directory (default: $SCAGENT_SDK_MODEL_PROFILES_DIR)",
    )


def _add_skills_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skills-root",
        help="skill-package directory (default: $SCAGENT_SDK_SKILLS_DIR or ./.claude/skills)",
    )


def _add_environments_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--environments-file",
        help="logical execution environments (default: $SCAGENT_SDK_ENVIRONMENTS_FILE)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scagent-sdk")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start a polished resumable analysis session")
    start.add_argument("--data", "-d", help="optional dataset to inspect at startup")
    start.add_argument("--prompt", help="optional initial request")
    start.add_argument("--title", default="Interactive analysis")
    start.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="SESSION_ID",
        help="resume a session; omit the ID to resume the most recent",
    )
    start.add_argument(
        "--profile",
        default=None,
        help=(
            "model profile (default: recorded profile when resuming, otherwise "
            "$SCAGENT_SDK_MODEL_PROFILE or iris-qwen36)"
        ),
    )
    start.add_argument("--cwd")
    start.add_argument("--gateway-config")
    start.add_argument("--no-auto-gateway", action="store_true")
    start.add_argument(
        "--thinking",
        choices=["enabled", "disabled", "adaptive", "native", "on", "off"],
        default=None,
        help="override reasoning generation mode (default: from profile/env)",
    )
    start.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="reasoning token budget when thinking is enabled",
    )
    start.add_argument(
        "--thinking-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="reasoning effort level",
    )
    thinking_display = start.add_mutually_exclusive_group()
    thinking_display.add_argument(
        "--show-thinking",
        action="store_true",
        dest="show_thinking",
        default=None,
        help="show model reasoning (dimmed) in the terminal",
    )
    thinking_display.add_argument(
        "--hide-thinking",
        action="store_false",
        dest="show_thinking",
        default=None,
        help="hide model reasoning in the terminal",
    )
    save_thinking = start.add_mutually_exclusive_group()
    save_thinking.add_argument(
        "--save-thinking",
        action="store_true",
        dest="save_thinking",
        default=None,
        help="persist reasoning to <session>/logs/reasoning.log",
    )
    save_thinking.add_argument(
        "--no-save-thinking",
        action="store_false",
        dest="save_thinking",
        default=None,
        help="do not persist reasoning to the session log",
    )
    _add_sessions_root(start)
    _add_profiles_root(start)
    _add_skills_root(start)
    _add_environments_file(start)
    start.set_defaults(handler=_cmd_start)

    session = commands.add_parser("session", help="manage durable scientific sessions")
    actions = session.add_subparsers(dest="session_command", required=True)

    new = actions.add_parser("new", help="create a scientific session")
    new.add_argument("--title", required=True)
    _add_sessions_root(new)
    new.set_defaults(handler=_cmd_session_new)

    listing = actions.add_parser("list", help="list scientific sessions")
    _add_sessions_root(listing)
    listing.set_defaults(handler=_cmd_session_list)

    show = actions.add_parser("show", help="show state and events")
    show.add_argument("session_id")
    _add_sessions_root(show)
    show.set_defaults(handler=_cmd_session_show)

    storage = actions.add_parser(
        "storage", help="report artifact storage and what a prune would reclaim (read-only)"
    )
    storage.add_argument(
        "session_id", nargs="?", help="one session; omit to report every session"
    )
    _add_sessions_root(storage)
    storage.set_defaults(handler=_cmd_session_storage)

    resume = actions.add_parser("resume", help="plan exact or reconstructed continuation")
    resume.add_argument("session_id")
    resume.add_argument("--runtime", default="claude-agent-sdk")
    resume.add_argument("--model-profile", default="local-default")
    _add_sessions_root(resume)
    _add_profiles_root(resume)
    resume.set_defaults(handler=_cmd_session_resume)

    bind = actions.add_parser("bind-runtime", help="record a resumable model conversation")
    bind.add_argument("session_id")
    bind.add_argument("--runtime", default="claude-agent-sdk")
    bind.add_argument("--runtime-session-id", required=True)
    bind.add_argument("--model-profile", required=True)
    bind.add_argument("--model-profile-fingerprint")
    bind.add_argument("--transport", required=True)
    bind.add_argument("--model")
    _add_sessions_root(bind)
    _add_profiles_root(bind)
    bind.set_defaults(handler=_cmd_session_bind_runtime)

    fork = actions.add_parser("fork", help="branch a scientific session")
    fork.add_argument("session_id")
    fork.add_argument("--title", required=True)
    fork.add_argument("--new-session-id")
    _add_sessions_root(fork)
    fork.set_defaults(handler=_cmd_session_fork)

    model = commands.add_parser("model", help="inspect model profiles")
    model_actions = model.add_subparsers(dest="model_command", required=True)
    model_list = model_actions.add_parser("list")
    _add_profiles_root(model_list)
    model_list.set_defaults(handler=_cmd_model_list)
    model_show = model_actions.add_parser("show")
    model_show.add_argument("profile")
    _add_profiles_root(model_show)
    model_show.set_defaults(handler=_cmd_model_show)

    capability = commands.add_parser("capability", help="inspect executable skill packages")
    capability_actions = capability.add_subparsers(dest="capability_command", required=True)
    capability_list = capability_actions.add_parser("list")
    _add_skills_root(capability_list)
    capability_list.set_defaults(handler=_cmd_capability_list)
    capability_validate = capability_actions.add_parser("validate")
    _add_skills_root(capability_validate)
    _add_environments_file(capability_validate)
    capability_validate.set_defaults(handler=_cmd_capability_validate)

    doctor = commands.add_parser("doctor", help="run compatibility diagnostics")
    doctor_actions = doctor.add_subparsers(dest="doctor_command", required=True)
    doctor_model = doctor_actions.add_parser("model")
    doctor_model.add_argument("--profile", default="local-default")
    doctor_model.add_argument("--timeout", type=float, default=5.0)
    doctor_model.add_argument("--no-network", action="store_true")
    doctor_model.add_argument("--probe-messages", action="store_true")
    _add_profiles_root(doctor_model)
    doctor_model.set_defaults(handler=_cmd_doctor_model)

    doctor_agent = doctor_actions.add_parser(
        "agent", help="run live text, tool, hook, retry, and context probes"
    )
    doctor_agent.add_argument("--profile", default="local-default")
    doctor_agent.add_argument(
        "--checks",
        nargs="+",
        choices=AVAILABLE_PROBES,
        default=list(AVAILABLE_PROBES),
    )
    doctor_agent.add_argument("--long-result-chars", type=int, default=49_152)
    doctor_agent.add_argument("--cwd")
    _add_sessions_root(doctor_agent)
    _add_profiles_root(doctor_agent)
    doctor_agent.set_defaults(handler=_cmd_doctor_agent)

    doctor_environment = doctor_actions.add_parser(
        "environment", help="verify configured scientific Python and GPU environments"
    )
    _add_environments_file(doctor_environment)
    doctor_environment.set_defaults(handler=_cmd_doctor_environment)

    agent = commands.add_parser("agent", help="run a model turn")
    agent_actions = agent.add_subparsers(dest="agent_command", required=True)
    run = agent_actions.add_parser("run")
    run.add_argument("session_id")
    run.add_argument("--prompt", required=True)
    run.add_argument("--profile", default="local-default")
    run.add_argument("--cwd")
    _add_sessions_root(run)
    _add_profiles_root(run)
    _add_skills_root(run)
    _add_environments_file(run)
    run.set_defaults(handler=_cmd_agent_run)

    chat = agent_actions.add_parser("chat", help="continue a resumable interactive session")
    chat.add_argument("session_id")
    chat.add_argument("--profile", default="local-default")
    chat.add_argument("--cwd")
    _add_sessions_root(chat)
    _add_profiles_root(chat)
    _add_skills_root(chat)
    _add_environments_file(chat)
    chat.set_defaults(handler=_cmd_agent_chat)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_project_environment()
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ScagentSDKError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
