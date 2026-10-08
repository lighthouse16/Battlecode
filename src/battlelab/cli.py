"""Battlelab cross-platform Command Line Interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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

    from battlelab.bots.process_runner import (
        get_containment_capabilities,
        get_memory_enforcement_details,
    )

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(
        f"Python Version:       {py_ver} ({'OK' if sys.version_info >= (3, 11) else 'WARN: <3.11'})"
    )
    print(f"Project Root:         {get_project_root()}")
    print(
        f"Data Directory:       {get_data_dir()} ({'Exists' if get_data_dir().exists() else 'Missing'})"
    )
    print(
        f"Database Path:        {get_database_path()} ({'Exists' if get_database_path().exists() else 'Missing'})"
    )
    mem_details = get_memory_enforcement_details()
    print(
        f"Memory Safeguards:    {mem_details['status']} ({mem_details['mechanism']}: {mem_details['detail']})"
    )

    # 2. Containment Capabilities
    print("\nContainment Capabilities:")
    containment = get_containment_capabilities()
    for name, info in containment.items():
        label = name.replace("_", " ").title()
        print(f"  - {label:<24} [{info['status']}] {info['mechanism']}")

    # 3. Adapters
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
        print("Capabilities:")
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
            entrypoint=args.entrypoint,
        )
        print("Registered Bot Artifact:")
        print(f"  Artifact ID:     {art.artifact_id}")
        print(f"  Display Name:    {art.display_name}")
        print(f"  Entrypoint:      {art.entrypoint_relpath}")
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
    print("Match Finished:")
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
        print(
            f"Tournament Finished: Status={res['status']} ({res['completed_matches']}/{res['total_matches']} complete)"
        )
        return 0

    elif args.action == "resume":
        print(f"Resuming Tournament {args.tournament_id}...")
        res = scheduler.run_tournament(args.tournament_id)
        print(
            f"Tournament Finished: Status={res['status']} ({res['completed_matches']}/{res['total_matches']} complete)"
        )
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
        print("Created Experiment:")
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
        actor = getattr(args, "actor", "human")
        override_reason = getattr(args, "override_reason", None)
        acknowledge_risk = getattr(args, "acknowledge_risk", None)
        try:
            res = gate.promote(
                args.experiment_id,
                dry_run=dry_run,
                actor=actor,
                override_reason=override_reason,
                acknowledge_risk=acknowledge_risk,
            )
            if dry_run:
                print(f"[DRY-RUN] Promotion check passed: Would promote {res['would_promote']}")
                if res.get("is_override"):
                    print(
                        "  WARNING: This dry-run was evaluated under human override acknowledgement!"
                    )
            else:
                print(
                    f"Successfully PROMOTED {res['champion_artifact_id']} as Champion! (Mode: {res.get('mode')})"
                )
            return 0
        except Exception as e:
            print(f"Promotion Failed: {e}")
            return 1

    return 1


def cmd_champion(args: argparse.Namespace) -> int:
    """Champion manifest inspection and rollback commands."""
    db = Database()
    registry = BotRegistry(db)
    gate = PromotionGate(db)

    if args.action == "status":
        champ = registry.get_champion_artifact()
        if not champ:
            print("No active champion artifact registered.")
            return 0
        print("Active Champion Artifact:")
        print(f"  Artifact ID:     {champ.artifact_id}")
        print(f"  Display Name:    {champ.display_name}")
        print(f"  Source Hash:     {champ.source_hash}")
        print(f"  Created At:      {champ.created_at}")
        return 0

    elif args.action == "init":
        manifest_path = get_champion_manifest_path()
        if manifest_path.exists():
            current_champ = registry.get_champion_artifact()
            if current_champ is not None:
                print(
                    f"Error: Champion is already initialized (current: {current_champ.artifact_id}). "
                    "Use 'battlelab experiment promote' or 'battlelab champion rollback'.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: Existing champion manifest found at {manifest_path} but active champion is invalid or corrupted. "
                    "First-initialization requires a clean, uninitialized state. "
                    "Explicit recovery required (inspect or remove corrupted manifest).",
                    file=sys.stderr,
                )
            return 1

        promotions = db.list_promotions()
        if promotions:
            print(
                "Error: Existing promotion history found in database. "
                "First-initialization requires a clean, uninitialized state. "
                "Use 'battlelab champion rollback' to restore a valid champion or inspect database.",
                file=sys.stderr,
            )
            return 1

        lock_path = manifest_path.with_suffix(".init.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with open(fd, "w", encoding="utf-8") as lock_f:
                lock_f.write(f"{os.getpid()}\n")
        except FileExistsError:
            print(
                "Error: Concurrent champion initialization detected or active initialization lock exists. "
                f"Lock file: {lock_path}",
                file=sys.stderr,
            )
            return 1

        try:
            if manifest_path.exists():
                print(
                    "Error: Champion manifest was created concurrently.",
                    file=sys.stderr,
                )
                return 1
            if db.list_promotions():
                print(
                    "Error: Promotion was recorded concurrently in database.",
                    file=sys.stderr,
                )
                return 1

            try:
                art = registry.get_artifact(args.artifact_id)
            except Exception:
                art = None
            if not art:
                print(f"Error: Artifact not found: {args.artifact_id}", file=sys.stderr)
                return 1

            from battlelab.bots.artifacts import verify_artifact_integrity

            ok, err = verify_artifact_integrity(art)
            if not ok:
                print(f"Error: Artifact integrity check failed: {err}", file=sys.stderr)
                return 1

            actor = getattr(args, "actor", "")
            if (
                not actor
                or not actor.strip()
                or actor.strip().lower() in ("human", "default", "unknown", "system", "root")
            ):
                print(
                    "Error: Initializing champion requires an explicit, named non-generic actor (e.g. researcher username).",
                    file=sys.stderr,
                )
                return 1

            reason = getattr(args, "reason", "")
            if not reason or len(reason.strip()) < 10:
                print(
                    "Error: Initializing champion requires an explicit reason (>= 10 characters).",
                    file=sys.stderr,
                )
                return 1

            from battlelab.official.readiness import OfficialReadinessChecker

            allow_unverified = getattr(args, "allow_unverified_adapter", False)
            checker = OfficialReadinessChecker()
            try:
                report = checker.evaluate()
                adapter_ready = report.can_run_local
            except Exception:
                report = None
                adapter_ready = False

            if not adapter_ready:
                if not allow_unverified:
                    blockers_str = (
                        f" (blockers: {', '.join(report.blockers[:5])})"
                        if (report and report.blockers)
                        else ""
                    )
                    print(
                        f"Error: Official adapter is not verified for local execution{blockers_str}. "
                        "Ordinary use cannot mark a candidate as official Champion v0 without verified official readiness. "
                        "To proceed during preliminary development without implied submission readiness, "
                        "explicitly specify --allow-unverified-adapter.",
                        file=sys.stderr,
                    )
                    return 1

                promotion_mode = "INITIAL_CHAMPION_V0_UNVERIFIED_OVERRIDE"
                override_ack: str | None = (
                    "OPERATOR_OVERRIDE: Initialized Champion v0 with unverified official adapter. "
                    "NO SUBMISSION READINESS IMPLIED."
                )
                gate_violations = (
                    report.blockers
                    if (report and report.blockers)
                    else ["OFFICIAL_ADAPTER_NOT_LOCAL_READY"]
                )
            else:
                promotion_mode = "INITIAL_CHAMPION_V0"
                override_ack = None
                gate_violations = []

            clean_actor = actor.strip()
            clean_reason = reason.strip()
            now_iso = datetime.now(timezone.utc).isoformat()
            init_reason = f"Initial Champion v0: {clean_reason}"

            promotion_id = (
                f"prom_init_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:8]}"
            )
            manifest_payload = {
                "champion_artifact_id": art.artifact_id,
                "experiment_id": None,
                "previous_champion_id": None,
                "updated_at": now_iso,
                "reason": init_reason,
            }

            try:
                db.save_promotion(
                    promotion_id=promotion_id,
                    experiment_id=None,
                    artifact_id=art.artifact_id,
                    promoted_at=now_iso,
                    manifest_snapshot=manifest_payload,
                    reason=init_reason,
                    mode=promotion_mode,
                    promoted_by=clean_actor,
                    override_acknowledgement=override_ack,
                    previous_champion_id=None,
                    gate_violations=gate_violations,
                    artifact_manifest_hash=art.manifest_hash,
                )
            except Exception as e:
                print(
                    f"Error: Failed to record promotion audit in database: {e}",
                    file=sys.stderr,
                )
                return 1

            try:
                registry.update_champion_manifest(
                    artifact_id=art.artifact_id,
                    experiment_id=None,
                    updated_at=now_iso,
                    reason=init_reason,
                    previous_champion_id=None,
                )
            except Exception as e:
                try:
                    with db.connect() as conn:
                        with conn:
                            conn.execute(
                                "DELETE FROM promotions WHERE promotion_id = ?",
                                (promotion_id,),
                            )
                except Exception:
                    pass
                print(f"Error: Failed to write champion manifest: {e}", file=sys.stderr)
                return 1

            print(f"Successfully INITIALIZED Champion v0 to {art.artifact_id} ({art.display_name})")
            print(f"  Promoted By: {clean_actor}")
            print(f"  Audit ID:    {promotion_id}")
            print(f"  Mode:        {promotion_mode}")
            if override_ack:
                print(f"  Notice:      {override_ack}")
            return 0
        finally:
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except OSError:
                pass

    elif args.action == "rollback":
        reason = getattr(args, "reason", "")
        actor = getattr(args, "actor", "")
        try:
            res = gate.rollback(args.artifact_id, reason=reason, actor=actor)
            print(
                f"Successfully ROLLED BACK champion to {res['champion_artifact_id']} (Reason: {reason})"
            )
            return 0
        except Exception as e:
            print(f"Rollback Failed: {e}")
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
    """Official competition SDK integration commands."""
    from battlelab.official.readiness import OfficialReadinessChecker
    from battlelab.official.sources import ingest_sources
    from battlelab.official.spec import init_game_spec, load_and_validate_spec

    checker = OfficialReadinessChecker()

    if args.action == "status":
        report = checker.evaluate()
        if getattr(args, "json", False):
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print("Official Competition Integration Status:")
            print(f"  Ready:            {'YES' if report.ready else 'NO'}")
            print(f"  Can Run Local:    {'YES' if report.can_run_local else 'NO'}")
            print(f"  Can Submit:       {'YES' if report.can_submit else 'NO'}")
            print(f"  Source Bundle:    {report.source_bundle_hash or 'None'}")
            print(f"  Spec Hash:        {report.spec_hash or 'None'}")
            print(f"  SDK Version:      {report.sdk_version or 'Unreleased'}")
            if report.blockers:
                print(f"  Blockers ({len(report.blockers)}):")
                for b in report.blockers:
                    print(f"    - {b}")
        if getattr(args, "check", False) and not report.ready:
            return 1
        return 0

    elif args.action == "readiness":
        report = checker.evaluate()
        if getattr(args, "json", False):
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print("=" * 70)
            print("OFFICIAL INTEGRATION READINESS CHECKLIST")
            print("=" * 70)
            for c in report.checks:
                status = "[PASS]" if c.passed else "[FAIL]"
                print(f"{status:<8} {c.name:<32} {c.details}")
            print("-" * 70)
            print(f"Overall Ready:      {report.ready}")
            print(f"Can Run Local:      {report.can_run_local}")
            print(f"Can Submit:         {report.can_submit}")
            if report.blockers:
                print(f"\nUnresolved Blockers ({len(report.blockers)}):")
                for b in report.blockers:
                    print(f"  * {b}")
        if getattr(args, "check", False) and not report.ready:
            return 1
        return 0

    elif args.action in ("ingest", "integrate"):
        src_path = Path(args.path)
        try:
            manifest = ingest_sources(src_path, copy_files=getattr(args, "copy", False))
            if getattr(args, "json", False):
                print(json.dumps(manifest.to_dict(), indent=2))
            else:
                print("Official Sources Ingestion Complete:")
                print(f"  Bundle Hash:      {manifest.bundle_hash}")
                print(f"  Source Path:      {manifest.source_path}")
                print(f"  Files Ingested:   {manifest.file_count}")
                print(f"  Total Bytes:      {manifest.total_size_bytes}")
                print(
                    f"  Manifest Path:    data/official/source_bundles/{manifest.bundle_hash}/source_manifest.json"
                )
            return 0
        except Exception as e:
            if getattr(args, "json", False):
                print(json.dumps({"error": str(e)}, indent=2))
            else:
                print(f"Error ingesting official sources: {e}")
            return 1

    elif args.action == "spec":
        spec_action = getattr(args, "spec_action", None)
        if spec_action == "init":
            try:
                spec = init_game_spec(args.source_bundle, Path(args.output))
                if getattr(args, "json", False):
                    print(json.dumps(spec.to_dict(), indent=2))
                else:
                    print(f"Initialized game specification at {args.output}")
                    print(f"  Source Bundle:    {spec.source_bundle_hash}")
                    print(f"  Spec Version:     {spec.spec_version}")
                    print(f"  Canonical Hash:   {spec.canonical_hash()}")
                return 0
            except Exception as e:
                print(f"Error initializing spec: {e}")
                return 1
        elif spec_action == "validate":
            is_valid, errors, spec_obj, is_ready = load_and_validate_spec(Path(args.path))
            canonical_h = spec_obj.canonical_hash() if spec_obj else None
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "valid": is_valid,
                            "activation_ready": is_ready,
                            "canonical_hash": canonical_h,
                            "errors": errors,
                        },
                        indent=2,
                    )
                )
            else:
                if is_valid:
                    print(f"Game specification at {args.path} is structurally valid.")
                    print(f"  Canonical Hash:   {canonical_h}")
                    print(f"  Activation Ready: {is_ready}")
                    if not is_ready:
                        print("  (Notice: Some rule items remain in MISSING state.)")
                else:
                    print(f"Game specification validation failed with {len(errors)} error(s):")
                    for err in errors:
                        print(f"  - {err}")
            return 0 if is_valid else 1

    elif args.action == "sdk":
        sdk_action = getattr(args, "sdk_action", None)
        if sdk_action == "probe":
            from battlelab.official.readiness import probe_engine_executable

            engine_path = getattr(args, "engine_path", None)
            as_json = getattr(args, "json", False)
            return probe_engine_executable(
                engine_path=engine_path,
                bridge=checker.bridge,
                as_json=as_json,
            )

    elif args.action == "activate":
        if getattr(args, "dry_run", False):
            dry_report = checker.dry_run_activation()
            if getattr(args, "json", False):
                print(json.dumps(dry_report, indent=2))
            else:
                print("Official Adapter Activation [DRY RUN]:")
                print(f"  Status:           {dry_report['status']}")
                print(f"  Message:          {dry_report['message']}")
                print(f"  Ready:            {dry_report['ready']}")
                print(f"  Source Bundle:    {dry_report['source_bundle_hash'] or 'None'}")
                print(f"  Spec Hash:        {dry_report['spec_hash'] or 'None'}")
                print(f"  SDK Version:      {dry_report['sdk_version'] or 'None'}")
                if dry_report["blockers"]:
                    print(f"  Blockers ({dry_report['blockers_count']}):")
                    for b in dry_report["blockers"]:
                        print(f"    - {b}")
            return 0
        else:
            msg = (
                "Non-dry-run activation is not yet implemented: "
                "persistent official adapter activation is disabled until competition launch."
            )
            if getattr(args, "json", False):
                print(json.dumps({"error": msg, "success": False, "activated": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

    return 1


def cmd_competition(args: argparse.Namespace) -> int:
    """Competition launch kit operational status."""
    if args.action == "status":
        from battlelab.config.loader import load_yaml_config
        from battlelab.official.readiness import OfficialReadinessChecker

        state_file = get_project_root() / "competition" / "COMPETITION_STATE.yaml"
        state: dict[str, Any] = {}
        if state_file.exists():
            try:
                state = load_yaml_config(state_file)
            except Exception as e:
                print(f"Warning: error loading competition state: {e}", file=sys.stderr)

        # Authoritative subsystem queries
        checker = OfficialReadinessChecker()
        report = checker.evaluate()

        registry = BotRegistry()
        champ = registry.get_champion_artifact()

        adapter_name = "official"
        try:
            adapter = get_adapter(adapter_name)
            caps = adapter.get_capabilities()
            adapter_info: dict[str, Any] = {
                "name": adapter.name,
                "can_run_local": caps.can_run_local,
                "can_submit": caps.can_submit,
            }
        except Exception as e:
            adapter_info = {
                "name": adapter_name,
                "error": str(e),
                "can_run_local": False,
                "can_submit": False,
            }

        status_data: dict[str, Any] = {
            "workflow": state.get("workflow", {}),
            "operator_decisions": state.get("operator_decisions", {}),
            "submission_controls": state.get("submission_controls", {}),
            "authoritative": {
                "official_readiness": {
                    "ready": report.ready,
                    "can_run_local": report.can_run_local,
                    "can_submit": report.can_submit,
                    "source_bundle_hash": report.source_bundle_hash,
                    "spec_hash": report.spec_hash,
                    "sdk_version": report.sdk_version,
                    "blockers_count": len(report.blockers),
                    "blockers": report.blockers,
                },
                "champion": {
                    "active": champ is not None,
                    "artifact_id": champ.artifact_id if champ else None,
                    "name": champ.display_name if champ else None,
                },
                "adapter": adapter_info,
            },
        }

        if getattr(args, "json", False):
            print(json.dumps(status_data, indent=2))
            return 0

        print("=" * 65)
        print("BATTLELAB COMPETITION STATUS")
        print("=" * 65)
        wf = status_data["workflow"]
        print("OPERATIONAL WORKFLOW PROGRESS (Operator-reported, Untrusted):")
        print(f"  Reported Phase:       {wf.get('current_phase', 'UNKNOWN')}")
        print(f"  Reported Status:      {wf.get('status', 'UNKNOWN')}")
        print(f"  Reported Next Action: {wf.get('next_action', 'None')}")
        print("-" * 65)
        print("AUTHORITATIVE SUBSYSTEM READINESS (System-verified):")
        off = status_data["authoritative"]["official_readiness"]
        print(f"  Official Integration:   {'READY' if off['ready'] else 'NOT READY'}")
        print(f"  Official Can Run Local: {'YES' if off['can_run_local'] else 'NO'}")
        print(f"  Official Can Submit:    {'YES' if off['can_submit'] else 'NO'}")
        print(f"  Source Bundle Hash:     {off['source_bundle_hash'] or 'None'}")
        print(f"  Game Spec Hash:         {off['spec_hash'] or 'None'}")
        print(f"  Official SDK Version:   {off['sdk_version'] or 'Unreleased'}")
        if off["blockers"]:
            print(f"  Active Blockers ({len(off['blockers'])}):")
            for b in off["blockers"][:5]:
                print(f"    - {b}")
            if len(off["blockers"]) > 5:
                print(f"    ... and {len(off['blockers']) - 5} more.")

        adp = status_data["authoritative"]["adapter"]
        adp_ready_str = (
            "READY" if (adp.get("can_run_local") and not adp.get("error")) else "NOT READY"
        )
        print(f"  Official Adapter:       {adp_ready_str} (name: {adp['name']})")

        champ_info = status_data["authoritative"]["champion"]
        champ_str = f"ACTIVE ({champ_info['artifact_id']})" if champ_info["active"] else "NONE"
        print(f"  Champion Artifact:      {champ_str}")

        sub = status_data["submission_controls"]
        approval = (
            "Required"
            if sub.get("operator_approval_required", True)
            else "DISALLOWED (must be required)"
        )
        print(f"  Submission Approval:    {approval}")
        print("=" * 65)
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
    p_bot_reg = p_bot_sub.add_parser(
        "register", help="Register a bot source into an immutable artifact"
    )
    p_bot_reg.add_argument("path", help="Path to bot source file or directory")
    p_bot_reg.add_argument("--name", help="Display name")
    p_bot_reg.add_argument(
        "--entrypoint", help="Relative path to entrypoint script within bot artifact"
    )
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
    p_tourn_run.add_argument(
        "--config", default="configs/evaluation.yaml", help="Evaluation config path"
    )
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
    p_exp_prom.add_argument(
        "--dry-run", action="store_true", help="Perform gate check without promoting"
    )
    p_exp_prom.add_argument(
        "--actor", default="human", help="Identity of person triggering promotion"
    )
    p_exp_prom.add_argument(
        "--override-reason", help="Mandatory justification if overriding failed gates"
    )
    p_exp_prom.add_argument(
        "--acknowledge-risk", help="Mandatory text acknowledgement (I_ACKNOWLEDGE_STATISTICAL_RISK)"
    )

    # champion
    p_champ = subparsers.add_parser("champion", help="Champion operations and rollback")
    p_champ_sub = p_champ.add_subparsers(dest="action", required=True)
    p_champ_sub.add_parser("status", help="View active champion")
    p_champ_init = p_champ_sub.add_parser(
        "init", help="Initialize Champion v0 when no champion exists"
    )
    p_champ_init.add_argument("artifact_id", help="Candidate baseline artifact ID")
    p_champ_init.add_argument(
        "--reason", required=True, help="Explicit meaningful reason for initialization"
    )
    p_champ_init.add_argument(
        "--actor", required=True, help="Explicit named identity of actor initializing champion"
    )
    p_champ_init.add_argument(
        "--allow-unverified-adapter",
        action="store_true",
        default=False,
        help="Explicit operator override to initialize Champion v0 before official adapter local verification is complete (audit logged, no submission readiness implied)",
    )
    p_champ_rb = p_champ_sub.add_parser(
        "rollback", help="Roll back champion to a historical artifact"
    )
    p_champ_rb.add_argument("artifact_id", help="Historical artifact ID")
    p_champ_rb.add_argument(
        "--reason", required=True, help="Explicit meaningful reason for rollback"
    )
    p_champ_rb.add_argument(
        "--actor", required=True, help="Explicit named identity of actor performing rollback"
    )

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

    # status
    p_off_stat = p_off_sub.add_parser("status", help="Official integration status")
    p_off_stat.add_argument("--json", action="store_true", help="Output status as JSON")
    p_off_stat.add_argument("--check", action="store_true", help="Exit nonzero if not ready")

    # readiness
    p_off_ready = p_off_sub.add_parser("readiness", help="Detailed readiness checklist")
    p_off_ready.add_argument("--json", action="store_true", help="Output checklist as JSON")
    p_off_ready.add_argument("--check", action="store_true", help="Exit nonzero if not ready")

    # ingest
    p_off_ing = p_off_sub.add_parser("ingest", help="Ingest authoritative official sources")
    p_off_ing.add_argument("path", help="Path to official document or directory")
    p_off_ing.add_argument("--copy", action="store_true", help="Copy source files into bundle")
    p_off_ing.add_argument("--json", action="store_true", help="Output manifest as JSON")

    # integrate (backward-compatible alias)
    p_off_int = p_off_sub.add_parser("integrate", help="Ingest official documentation")
    p_off_int.add_argument("path", help="Path to official documentation")
    p_off_int.add_argument("--copy", action="store_true", help="Copy source files into bundle")
    p_off_int.add_argument("--json", action="store_true", help="Output manifest as JSON")

    # spec
    p_off_spec = p_off_sub.add_parser("spec", help="Game specification management")
    p_off_spec_sub = p_off_spec.add_subparsers(dest="spec_action", required=True)

    p_off_spec_init = p_off_spec_sub.add_parser("init", help="Initialize typed game specification")
    p_off_spec_init.add_argument(
        "--source-bundle", required=True, help="Ingested source bundle hash"
    )
    p_off_spec_init.add_argument(
        "--output", required=True, help="Path to write game specification YAML"
    )
    p_off_spec_init.add_argument("--json", action="store_true", help="Output spec as JSON")

    p_off_spec_val = p_off_spec_sub.add_parser("validate", help="Validate typed game specification")
    p_off_spec_val.add_argument("path", help="Path to game specification YAML")
    p_off_spec_val.add_argument("--json", action="store_true", help="Output validation as JSON")

    # sdk
    p_off_sdk = p_off_sub.add_parser("sdk", help="Official SDK operations")
    p_off_sdk_sub = p_off_sdk.add_subparsers(dest="sdk_action", required=True)
    p_off_sdk_probe = p_off_sdk_sub.add_parser("probe", help="Probe official SDK")
    p_off_sdk_probe.add_argument(
        "engine_path",
        nargs="?",
        default=None,
        help="Path to official engine executable to probe",
    )
    p_off_sdk_probe.add_argument("--json", action="store_true", help="Output probe as JSON")

    # activate
    p_off_act = p_off_sub.add_parser("activate", help="Activate official competition adapter")
    p_off_act.add_argument("--dry-run", action="store_true", help="Dry run check without changes")
    p_off_act.add_argument("--acknowledge-sdk", help="Explicit acknowledgement token")
    p_off_act.add_argument("--json", action="store_true", help="Output activation report as JSON")

    # competition
    p_comp = subparsers.add_parser("competition", help="Competition launch kit operations")
    p_comp_sub = p_comp.add_subparsers(dest="action", required=True)
    p_comp_stat = p_comp_sub.add_parser("status", help="Competition operational state")
    p_comp_stat.add_argument("--json", action="store_true", help="Output status as JSON")

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
    elif parsed.command == "champion":
        return cmd_champion(parsed)
    elif parsed.command == "replay":
        return cmd_replay(parsed)
    elif parsed.command == "official":
        return cmd_official(parsed)
    elif parsed.command == "competition":
        return cmd_competition(parsed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
