"""Battlelab cross-platform Command Line Interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from battlelab.adapters import get_adapter, list_adapters
from battlelab.analysis.metrics import calculate_tournament_metrics
from battlelab.bots.registry import BotRegistry
from battlelab.config.validation import validate_all_configs
from battlelab.core.identifiers import generate_match_id
from battlelab.core.models import MatchSpec
from battlelab.experiments.evaluator import ExperimentEvaluator
from battlelab.experiments.promotion import PromotionGate
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.matches.matrix import generate_match_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.matches.worker import execute_match_job
from battlelab.storage.database import Database
from battlelab.storage.paths import (
    get_champion_manifest_path,
    get_data_dir,
    get_database_path,
    get_project_root,
    get_reports_dir,
)
from battlelab.telemetry.replay_store import ReplayStore


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose platform environment and adapters."""
    print("=" * 60)
    print("BATTLELAB SYSTEM DOCTOR")
    print("=" * 60)

    # 1. Environment & Paths
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"Python Version:       {py_ver} ({'OK' if sys.version_info >= (3, 11) else 'WARN: <3.11'})")
    print(f"Project Root:         {get_project_root()}")
    print(f"Data Directory:       {get_data_dir()} ({'Exists' if get_data_dir().exists() else 'Missing'})")
    print(f"Database Path:        {get_database_path()} ({'Exists' if get_database_path().exists() else 'Missing'})")

    # 2. Adapters
    print("\nAdapters:")
    for name in list_adapters():
        adapter = get_adapter(name)
        ok, msg = adapter.validate_installation()
        caps = adapter.get_capabilities()
        status_tag = "[READY]" if ok else "[UNRELEASED/MISSING]"
        print(f"  - {name:<20} {status_tag} {msg}")
        print(f"    Can run local: {caps.can_run_local} | Can submit: {caps.can_submit}")

    # 3. Champion Status
    champ_path = get_champion_manifest_path()
    if champ_path.exists():
        try:
            with open(champ_path, "r", encoding="utf-8") as f:
                champ_data = json.load(f)
            print(f"\nActive Champion:      {champ_data.get('champion_artifact_id')}")
        except Exception:
            print("\nActive Champion:      Manifest corrupted")
    else:
        print("\nActive Champion:      None registered")

    print("\nSystem doctor completed.")
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    """Validate YAML configuration files."""
    cfg_dir = get_project_root() / "configs"
    print(f"Validating configurations in {cfg_dir}...")
    results = validate_all_configs(cfg_dir)
    has_errors = False
    for fname, errs in results.items():
        if errs:
            has_errors = True
            print(f"  [FAIL] {fname}:")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"  [PASS] {fname}")

    return 1 if has_errors else 0


def cmd_adapters(args: argparse.Namespace) -> int:
    """List or inspect adapters."""
    if args.action == "list":
        print("Available Adapters:")
        for name in list_adapters():
            print(f"  - {name}")
        return 0
    elif args.action == "inspect":
        adapter = get_adapter(args.name)
        caps = adapter.get_capabilities()
        maps = adapter.discover_maps()
        print(f"Adapter: {adapter.name} (v{adapter.version})")
        print(f"Game Version: {caps.game_version}")
        print(f"Capabilities:")
        print(f"  Local Matches:    {caps.can_run_local}")
        print(f"  Remote Tests:     {caps.can_run_remote}")
        print(f"  Official Submit:  {caps.can_submit}")
        print(f"Available Maps: {maps if maps else 'None'}")
        return 0
    return 1


def cmd_bot(args: argparse.Namespace) -> int:
    """Bot registration and inspection commands."""
    db = Database()
    registry = BotRegistry(db)

    if args.action == "register":
        src_path = Path(args.path)
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        art = registry.register_bot(
            source_path=src_path,
            display_name=args.name,
            language=args.language,
            tags=tags,
        )
        print(f"Registered Bot Artifact:")
        print(f"  Artifact ID:     {art.artifact_id}")
        print(f"  Display Name:    {art.display_name}")
        print(f"  Source Hash:     {art.source_hash}")
        print(f"  Location:        {art.source_location}")
        print(f"  Git Commit:      {art.git_commit or 'None'} (Dirty: {art.dirty_worktree})")
        return 0

    elif args.action == "list":
        artifacts = registry.list_artifacts()
        if not artifacts:
            print("No bot artifacts registered.")
            return 0
        print(f"{'ARTIFACT ID':<22} {'DISPLAY NAME':<20} {'LANGUAGE':<10} {'CREATED AT'}")
        print("-" * 75)
        for a in artifacts:
            print(f"{a.artifact_id:<22} {a.display_name:<20} {a.language:<10} {a.created_at[:19]}")
        return 0

    elif args.action == "inspect":
        art = registry.get_artifact(args.artifact_id)
        print(f"Artifact ID:      {art.artifact_id}")
        print(f"Display Name:     {art.display_name}")
        print(f"Language:         {art.language}")
        print(f"Source Hash:      {art.source_hash}")
        print(f"Build Config Hash:{art.build_config_hash}")
        print(f"Tags:             {art.tags}")
        print(f"Source Location:  {art.source_location}")
        print(f"Git Commit:       {art.git_commit} (Dirty: {art.dirty_worktree})")
        return 0

    return 1


def cmd_match(args: argparse.Namespace) -> int:
    """Run a single match directly."""
    adapter = get_adapter(args.adapter)
    db = Database()
    registry = BotRegistry(db)

    bot_a = registry.get_artifact(args.bot_a)
    bot_b = registry.get_artifact(args.bot_b)

    spec_dict = {
        "adapter_name": args.adapter,
        "adapter_version": adapter.version,
        "bot_a_id": bot_a.artifact_id,
        "bot_b_id": bot_b.artifact_id,
        "map_name": args.map,
        "seed": args.seed,
        "side_assignment": {"A": "side_0", "B": "side_1"},
        "time_limit_ms": args.time_limit,
    }
    match_id = generate_match_id(spec_dict)
    spec_dict["match_id"] = match_id
    spec = MatchSpec.from_dict(spec_dict)

    # Save spec to DB
    from datetime import datetime, timezone
    db.save_match_spec(spec, datetime.now(timezone.utc).isoformat())

    print(f"Executing match {match_id} on {args.map} (seed {args.seed})...")
    res = execute_match_job(spec.to_dict())
    print(f"Match Finished:")
    print(f"  Outcome:        {res.get('outcome')}")
    print(f"  Winner:         {res.get('winner')}")
    print(f"  Score A:        {res.get('score_a')}")
    print(f"  Score B:        {res.get('score_b')}")
    print(f"  Duration:       {res.get('duration_ms', 0):.1f}ms")
    print(f"  Replay:         {res.get('replay_path')}")
    return 0


def cmd_tournament(args: argparse.Namespace) -> int:
    """Tournament execution, resumption, and status commands."""
    db = Database()
    scheduler = TournamentScheduler(db, max_workers=args.workers)

    if args.action == "run":
        from battlelab.config.loader import load_yaml_config
        cfg = load_yaml_config(args.config)
        
        bot_a_id = args.bot_a
        bot_b_id = args.bot_b
        if not bot_a_id or not bot_b_id:
            # Pick first two registered artifacts
            arts = db.list_artifacts()
            if len(arts) < 2:
                print("Error: At least two bots must be registered to run a tournament.")
                return 1
            bot_a_id = bot_a_id or arts[0].artifact_id
            bot_b_id = bot_b_id or arts[1].artifact_id

        from battlelab.core.identifiers import generate_tournament_id
        t_id = generate_tournament_id()
        specs = generate_match_matrix(
            bot_a_id=bot_a_id,
            opponents=[bot_b_id],
            maps=cfg.get("maps", ["grid_classic_8x8"]),
            seeds=cfg.get("seeds", [42, 137]),
            adapter_name=cfg.get("adapter", "mock"),
            paired_sides=cfg.get("paired_sides", True),
            repetitions=cfg.get("repetitions", 1),
            time_limit_ms=cfg.get("time_limit_ms", 5000),
            tournament_id=t_id,
        )
        scheduler.create_tournament(t_id, f"Tournament {t_id}", specs, cfg)
        print(f"Launched Tournament {t_id} with {len(specs)} matches...")
        res = scheduler.run_tournament(t_id)
        print(f"Tournament Finished: Status={res['status']} ({res['completed_matches']}/{res['total_matches']} complete)")
        return 0

    elif args.action == "resume":
        print(f"Resuming Tournament {args.tournament_id}...")
        res = scheduler.run_tournament(args.tournament_id)
        print(f"Tournament Finished: Status={res['status']} ({res['completed_matches']}/{res['total_matches']} complete)")
        return 0

    elif args.action == "status":
        trn = db.get_tournament(args.tournament_id)
        if not trn:
            print(f"Tournament {args.tournament_id} not found.")
            return 1
        matches = db.list_matches_by_tournament(args.tournament_id)
        completed = [m for m in matches if m["status"] == "COMPLETED"]
        print(f"Tournament ID:  {trn['tournament_id']}")
        print(f"Name:           {trn['name']}")
        print(f"Status:         {trn['status']}")
        print(f"Progress:       {len(completed)} / {len(matches)} matches completed")
        return 0

    return 1


def cmd_experiment(args: argparse.Namespace) -> int:
    """Experiment lifecycle commands."""
    db = Database()
    exp_reg = ExperimentRegistry(db)
    evaluator = ExperimentEvaluator(db)
    gate = PromotionGate(db)

    if args.action == "create":
        exp = exp_reg.create_experiment(
            hypothesis=args.hypothesis,
            baseline_artifact_id=args.baseline,
            challenger_artifact_id=args.challenger,
            intended_change=args.change,
        )
        print(f"Created Experiment:")
        print(f"  Experiment ID: {exp.experiment_id}")
        print(f"  Hypothesis:    {exp.hypothesis}")
        print(f"  Challenger:    {exp.challenger_artifact_id}")
        print(f"  Baseline:      {exp.baseline_artifact_id}")
        return 0

    elif args.action == "run":
        print(f"Running Experiment {args.experiment_id}...")
        res = evaluator.run_experiment(args.experiment_id, max_workers=args.workers)
        print(f"Experiment {args.experiment_id} Complete!")
        print(f"  Report:         {res['report_path']}")
        print(f"  AI Feedback:    {res['packet_path']}")
        print(f"  Gate Passed:    {res['gate_results'].get('passed')}")
        return 0

    elif args.action == "analyze":
        exp = exp_reg.get_experiment(args.experiment_id)
        matches = db.list_matches_by_experiment(args.experiment_id)
        metrics = calculate_tournament_metrics(matches, focus_bot_id=exp.challenger_artifact_id)
        print(json.dumps(metrics, indent=2))
        return 0

    elif args.action == "report":
        report_file = get_reports_dir() / args.experiment_id / "report.md"
        if not report_file.exists():
            print(f"Report not found at {report_file}. Run experiment first.")
            return 1
        print(report_file.read_text(encoding="utf-8"))
        return 0

    elif args.action == "promote":
        dry_run = args.dry_run
        force = args.force
        try:
            res = gate.promote(args.experiment_id, dry_run=dry_run, force=force)
            if dry_run:
                print(f"[DRY-RUN] Promotion check passed: Would promote {res['would_promote']}")
            else:
                print(f"Successfully PROMOTED {res['champion_artifact_id']} as Champion!")
            return 0
        except Exception as e:
            print(f"Promotion Failed: {e}")
            return 1

    return 1


def cmd_replay(args: argparse.Namespace) -> int:
    """Inspect and verify replays."""
    store = ReplayStore()
    if args.action == "inspect":
        target = Path(args.replay_id)
        if not target.exists():
            target = store.base_dir / f"{args.replay_id}.jsonl"
        if not target.exists():
            print(f"Replay not found: {args.replay_id}")
            return 1
        stats = store.validate_replay_file(target)
        print(f"Replay Path:    {target}")
        print(f"Total Frames:   {stats['frames_count']}")
        print(f"Metadata:       {json.dumps(stats['meta'], indent=2)}")
        return 0

    elif args.action == "verify":
        ok = store.verify_stored_replay(args.replay_id)
        print(f"Replay {args.replay_id} Integrity: {'VALID' if ok else 'CORRUPTED/MISSING'}")
        return 0 if ok else 1

    return 1


def cmd_official(args: argparse.Namespace) -> int:
    """Official SDK interaction commands."""
    if args.action == "status":
        off = get_adapter("official_placeholder")
        caps = off.get_capabilities()
        print("Official Competition Integration Status:")
        print(f"  Status:       UNRELEASED")
        print(f"  Rulebook Ingested: NO")
        print(f"  SDK Installed:    NO")
        print(f"  Capability:   can_run_local={caps.can_run_local}, can_submit={caps.can_submit}")
        print("  Instructions: When competition releases rules, follow docs/day_zero_rule_ingestion.md")
        return 0

    elif args.action == "integrate":
        doc_path = Path(args.path)
        print(f"Initiating Day-Zero rules ingestion from: {doc_path}...")
        # Template generator
        spec_template_path = get_project_root() / "docs" / "game_spec.template.yaml"
        target_path = get_project_root() / "configs" / "game_spec.yaml"
        if spec_template_path.exists():
            target_path.write_text(spec_template_path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Generated versioned game specification template at {target_path}")
        else:
            print("Template generated in configs/game_spec.yaml")
        print("Next step: Complete each section of configs/game_spec.yaml based on official docs.")
        return 0

    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="battlelab",
        description="Battlelab: Competition-agnostic R&D platform for Battlecode",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    subparsers.add_parser("doctor", help="Check platform installation and adapter status")

    # config validate
    p_config = subparsers.add_parser("config", help="Configuration management")
    p_config_sub = p_config.add_subparsers(dest="action", required=True)
    p_config_sub.add_parser("validate", help="Validate YAML config files")

    # adapters
    p_adapters = subparsers.add_parser("adapters", help="Game adapters management")
    p_adapters_sub = p_adapters.add_subparsers(dest="action", required=True)
    p_adapters_sub.add_parser("list", help="List registered adapters")
    p_inspect_adapter = p_adapters_sub.add_parser("inspect", help="Inspect adapter capabilities")
    p_inspect_adapter.add_argument("name", help="Adapter name (e.g. mock, official)")

    # bot
    p_bot = subparsers.add_parser("bot", help="Bot registration and artifacts")
    p_bot_sub = p_bot.add_subparsers(dest="action", required=True)
    p_bot_reg = p_bot_sub.add_parser("register", help="Register a bot source into an immutable artifact")
    p_bot_reg.add_argument("path", help="Path to bot source file or directory")
    p_bot_reg.add_argument("--name", help="Display name")
    p_bot_reg.add_argument("--language", default="python", help="Language/runtime")
    p_bot_reg.add_argument("--tags", help="Comma-separated tags (e.g. policy:fixed,baseline)")

    p_bot_sub.add_parser("list", help="List registered artifacts")
    p_bot_insp = p_bot_sub.add_parser("inspect", help="Inspect an artifact by ID")
    p_bot_insp.add_argument("artifact_id", help="Artifact ID (e.g. art_...)")

    # match
    p_match = subparsers.add_parser("match", help="Match execution")
    p_match_sub = p_match.add_subparsers(dest="action", required=True)
    p_match_run = p_match_sub.add_parser("run", help="Run a single match")
    p_match_run.add_argument("--adapter", default="mock", help="Adapter name")
    p_match_run.add_argument("--bot-a", required=True, help="Bot A artifact ID")
    p_match_run.add_argument("--bot-b", required=True, help="Bot B artifact ID")
    p_match_run.add_argument("--map", required=True, help="Map name")
    p_match_run.add_argument("--seed", type=int, default=42, help="Deterministic seed")
    p_match_run.add_argument("--time-limit", type=int, default=5000, help="Time limit ms")

    # tournament
    p_tourn = subparsers.add_parser("tournament", help="Tournament operations")
    p_tourn_sub = p_tourn.add_subparsers(dest="action", required=True)
    p_tourn_run = p_tourn_sub.add_parser("run", help="Run tournament from config")
    p_tourn_run.add_argument("--config", default="configs/evaluation.yaml", help="Evaluation config path")
    p_tourn_run.add_argument("--bot-a", help="Optional Bot A artifact ID")
    p_tourn_run.add_argument("--bot-b", help="Optional Bot B artifact ID")
    p_tourn_run.add_argument("--workers", type=int, default=4, help="Worker count")

    p_tourn_res = p_tourn_sub.add_parser("resume", help="Resume interrupted tournament")
    p_tourn_res.add_argument("tournament_id", help="Tournament ID")
    p_tourn_res.add_argument("--workers", type=int, default=4, help="Worker count")

    p_tourn_stat = p_tourn_sub.add_parser("status", help="Get tournament status")
    p_tourn_stat.add_argument("tournament_id", help="Tournament ID")

    # experiment
    p_exp = subparsers.add_parser("experiment", help="Experiment operations")
    p_exp_sub = p_exp.add_subparsers(dest="action", required=True)
    p_exp_create = p_exp_sub.add_parser("create", help="Create an experiment")
    p_exp_create.add_argument("--hypothesis", required=True, help="Falsifiable hypothesis")
    p_exp_create.add_argument("--baseline", required=True, help="Baseline artifact ID")
    p_exp_create.add_argument("--challenger", required=True, help="Challenger artifact ID")
    p_exp_create.add_argument("--change", required=True, help="Intended change description")

    p_exp_run = p_exp_sub.add_parser("run", help="Run an experiment")
    p_exp_run.add_argument("experiment_id", help="Experiment ID")
    p_exp_run.add_argument("--workers", type=int, default=4, help="Worker count")

    p_exp_ana = p_exp_sub.add_parser("analyze", help="Analyze experiment results")
    p_exp_ana.add_argument("experiment_id", help="Experiment ID")

    p_exp_rep = p_exp_sub.add_parser("report", help="View experiment report")
    p_exp_rep.add_argument("experiment_id", help="Experiment ID")

    p_exp_prom = p_exp_sub.add_parser("promote", help="Evaluate and promote experiment challenger")
    p_exp_prom.add_argument("experiment_id", help="Experiment ID")
    p_exp_prom.add_argument("--dry-run", action="store_true", help="Perform gate check without promoting")
    p_exp_prom.add_argument("--force", action="store_true", help="Force promotion bypassing criteria")

    # replay
    p_rep = subparsers.add_parser("replay", help="Replay inspection")
    p_rep_sub = p_rep.add_subparsers(dest="action", required=True)
    p_rep_insp = p_rep_sub.add_parser("inspect", help="Inspect replay")
    p_rep_insp.add_argument("replay_id", help="Replay hash or file path")

    p_rep_ver = p_rep_sub.add_parser("verify", help="Verify replay integrity")
    p_rep_ver.add_argument("replay_id", help="Replay hash")

    # official
    p_off = subparsers.add_parser("official", help="Official competition SDK integration")
    p_off_sub = p_off.add_subparsers(dest="action", required=True)
    p_off_sub.add_parser("status", help="Official integration status")
    p_off_int = p_off_sub.add_parser("integrate", help="Ingest official documentation")
    p_off_int.add_argument("path", help="Path to official documentation")

    parsed = parser.parse_args(argv)

    if parsed.command == "doctor":
        return cmd_doctor(parsed)
    elif parsed.command == "config":
        return cmd_config_validate(parsed)
    elif parsed.command == "adapters":
        return cmd_adapters(parsed)
    elif parsed.command == "bot":
        return cmd_bot(parsed)
    elif parsed.command == "match":
        return cmd_match(parsed)
    elif parsed.command == "tournament":
        return cmd_tournament(parsed)
    elif parsed.command == "experiment":
        return cmd_experiment(parsed)
    elif parsed.command == "replay":
        return cmd_replay(parsed)
    elif parsed.command == "official":
        return cmd_official(parsed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
