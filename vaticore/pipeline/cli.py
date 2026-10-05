"""Command line for the daily pipeline.

    uv run python -m vaticore.pipeline ingest                   # pull new readings
    uv run python -m vaticore.pipeline run --channel whatsapp   # plan and send (daily)
    uv run python -m vaticore.pipeline run --channel whatsapp --channel email
    uv run python -m vaticore.pipeline score                    # score finished days
    uv run python -m vaticore.pipeline scorecard                # track record per site
    uv run python -m vaticore.pipeline report --operator example-towerco \
        --baseline 2026-09-01:2026-09-30 --pilot 2026-10-01:2026-10-31 --control kog-rur-0077
    uv run python -m vaticore.pipeline summary --channel whatsapp   # supervisors' week (Mondays)
    uv run python -m vaticore.pipeline safety-net --channel whatsapp  # nobody left without a message
    uv run python -m vaticore.pipeline site check --portfolio new.toml  # before uploading sites
    uv run python -m vaticore.pipeline replies --days 7         # 1s, 2s, reasons, STOPs
    uv run python -m vaticore.pipeline shadow-review --operator example-towerco --days 28
    uv run python -m vaticore.pipeline monitor                  # each model, week by week
    uv run python -m vaticore.pipeline fuel add --site OP/SITE --litres 500 --at 2026-10-06T10:30+01:00
    uv run python -m vaticore.pipeline fuel report --days 30    # delivered against burned
    uv run python -m vaticore.pipeline health --days 30         # data health per site
    uv run python -m vaticore.pipeline apikey create --operator example-towerco --name ops
    uv run python -m vaticore.pipeline optout --email someone@example.com
    uv run python -m vaticore.pipeline whatsapp-test --to +234...
    uv run python -m vaticore.pipeline email-test --to someone@example.com
    uv run python -m vaticore.pipeline demo                     # full loop, synthetic
    uv run python -m vaticore.pipeline migrate                  # apply schema migrations
    uv run python -m vaticore.pipeline backup --out /backups --verify

Settings (portfolio, recipients, sources, database, credentials) come from the
environment or .env (see .env.example); flags override them. run and ingest
exit with status 1 if any site failed, so a scheduler can alert on it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from vaticore.config import Settings, get_settings
from vaticore.delivery.channels import Channel, ConsoleChannel, EmailChannel, WhatsAppChannel
from vaticore.delivery.message import PlanMessage
from vaticore.delivery.recipients import Recipient, email_hash, load_recipients, recipient_hash
from vaticore.features.weather import OpenMeteoProvider
from vaticore.ingestion.connectors import load_sources, sync_sources
from vaticore.observability import heartbeat, init_error_tracking, setup_logging
from vaticore.pipeline import monitoring
from vaticore.pipeline.health import site_health_report
from vaticore.pipeline.runner import run_portfolio
from vaticore.pipeline.scoring import score_due, scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Portfolio, load_portfolio
from vaticore.storage import get_repository, migrations
from vaticore.storage.backup import backup, prune, verify_restore

DEFAULT_PORTFOLIO = Path("examples/sites/nigeria_portfolio.toml")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings)
    init_error_tracking(settings, f"pipeline-{args.command}")

    if args.command == "migrate":
        return _migrate(args, settings)
    if args.command == "backup":
        return _backup(args, settings)
    if args.command == "restore-check":
        scratch = args.scratch or (
            settings.restore_test_url.get_secret_value() if settings.restore_test_url else None
        )
        check = verify_restore(args.dump, scratch)
        print(check.to_text())
        return 0 if check.ok else 1

    if args.command == "site" and not args.with_data:
        return _site_check(args, settings, None)

    if args.command == "demo":
        from vaticore.pipeline.demo import run_demo

        run_demo(load_portfolio(args.portfolio), days=args.days)
        return 0
    if args.command == "whatsapp-test":
        result = _whatsapp(settings, "template").send_hello_world(args.to)
        print(f"{result.status}: {result.provider_message_id or result.error}")
        return 0 if result.status == "sent" else 1
    if args.command == "email-test":
        test = PlanMessage(
            "a test site", "today", "not needed today.", "not counted on today.", "ends 50%.",
            "This is a test of Vaticore's email delivery.",
        )  # fmt: skip
        result = _email(settings).send(args.to, test)
        print(f"{result.status}: {result.provider_message_id or result.error}")
        return 0 if result.status == "sent" else 1

    store = PlanStore(settings.plan_store_url or settings.database_url)
    try:
        if args.command == "fuel" and args.action in ("add", "import"):
            return _fuel_record(args, store)
        if args.command == "apikey":
            return _apikey(args, store)
        if args.command == "optout":
            return _optout(args, store)
        if args.command == "replies":
            return _replies(args, store)

        repo = get_repository(settings)
        try:
            if args.command == "site":
                return _site_check(args, settings, repo)
            if args.command == "ingest":
                path = args.sources or settings.sources_file
                if path is None:
                    raise SystemExit("ingest needs --sources or VATICORE_SOURCES_FILE")
                results = sync_sources(load_sources(path), repo)
                for r in results:
                    status = f"error: {r.error}" if r.error else f"{r.rows} rows"
                    print(f"{r.operator_id}/{r.site_id} ({r.connector}): {status}")
                    for note in r.notes:
                        print(f"  {note}")
                return 1 if any(r.error for r in results) else 0

            portfolio = _portfolio(args, settings)
            if args.command == "score":
                for record in score_due(portfolio, repo, store):
                    print(
                        f"{record.operator_id}/{record.site_id} {record.plan_date}: "
                        f"{record.hours_scored} h scored, plan {record.fuel_plan_l:.0f} L, "
                        f"baseline {record.fuel_baseline_l:.0f} L"
                    )
                return 0
            if args.command == "fuel":
                return _fuel_report(args, portfolio, repo, store)
            if args.command == "report":
                return _savings_report(args, settings, portfolio, repo, store)
            if args.command == "summary":
                return _summaries(args, settings, portfolio, repo, store)
            if args.command == "shadow-review":
                return _shadow_review(args, portfolio, store)
            if args.command == "monitor":
                for site in portfolio.sites:
                    _monitor(store, site.operator_id, site.site_id, args.weeks)
                return 0
            if args.command == "scorecard":
                for site in portfolio.sites:
                    card = scorecard(store, site.operator_id, site.site_id, days=args.days)
                    print(card.summary())
                return 0
            if args.command == "health":
                end = pd.Timestamp(datetime.now(tz=UTC))
                worst = 0
                for site in portfolio.sites:
                    raw = repo.read_history(site.operator_id, site.site_id)
                    report = site_health_report(
                        raw,
                        operator_id=site.operator_id,
                        site_id=site.site_id,
                        end=end,
                        days=args.days,
                        timezone=site.timezone,
                        longitude=site.longitude,
                        has_solar=site.solar is not None,
                        has_grid=site.grid is not None and not site.grid.reliable,
                        has_battery=site.battery.usable_kwh > 0,
                    )
                    print(report.to_text() + "\n")
                    worst = max(worst, {"ok": 0, "warn": 0, "fail": 1}[report.status.value])
                return worst
            if args.command == "safety-net":
                return _safety_net(args, settings, portfolio, store)
            code = _run(args, settings, portfolio, repo, store)
            heartbeat(settings, ok=code == 0)
            return code
        finally:
            repo.close()
    finally:
        store.close()


def _run(
    args: argparse.Namespace,
    settings: Settings,
    portfolio: Portfolio,
    repo: object,
    store: PlanStore,
) -> int:
    channels: list[Channel] = []
    for name in args.channel or []:
        if name == "console":
            channels.append(ConsoleChannel())
        elif name == "whatsapp":
            channels.append(_whatsapp(settings, args.whatsapp_mode))
        elif name == "email":
            channels.append(_email(settings))
    recipients = _recipients(args, settings) if channels else []
    sites = [_site_key(s) for s in args.site] if args.site else None
    weather = None if args.no_weather else _weather(settings)
    runs = run_portfolio(
        portfolio,
        repo,  # type: ignore[arg-type]
        store,
        plan_date=args.date,
        sites=sites,
        channels=channels,
        recipients=recipients,
        force=args.force,
        weather=weather,
    )
    for r in runs:
        sent = ", ".join(f"{who} {status}" for who, status in r.deliveries) or "not sent"
        print(f"{r.operator_id}/{r.site_id} {r.plan_date}: {r.status} ({r.model}); {sent}")
        if r.error:
            print(f"  error: {r.error}")
    alerts = [alert for r in runs for alert in r.alerts]
    for alert in alerts:
        print(f"ALERT {alert}")
    if alerts:
        _alert_ops(settings, alerts)
    return 1 if any(r.status == "failed" for r in runs) else 0


def _site_check(args: argparse.Namespace, settings: Settings, repo: object | None) -> int:
    from vaticore.sites.check import Level, check_portfolio_file, check_readiness

    path = args.portfolio or settings.portfolio_file or DEFAULT_PORTFOLIO
    checks = check_portfolio_file(path)
    if repo is not None:
        recipients = None
        if args.recipients or settings.recipients_file:
            recipients = _recipients(args, settings)
        sources_path = args.sources or settings.sources_file
        keys = (
            {(s.operator_id, s.site_id) for s in load_sources(sources_path)}
            if sources_path is not None
            else None
        )
        end = pd.Timestamp(datetime.now(tz=UTC))
        for check in checks:
            site = check.site
            if site is None:
                continue
            sourced = None if keys is None else site.key in keys
            readings = repo.read_history(  # type: ignore[attr-defined]
                site.operator_id, site.site_id, end - pd.Timedelta(days=60), end
            )
            check.findings.extend(
                check_readiness(site, readings, recipients=recipients, sourced=sourced)
            )
    if args.site:
        wanted = {f"{o}/{s}" for o, s in (_site_key(v) for v in args.site)}
        checks = [c for c in checks if c.label in wanted]
    for check in checks:
        print(check.to_text() + "\n")
    failed = sum(c.status is Level.FAIL for c in checks)
    warned = sum(c.status is Level.WARN for c in checks)
    print(f"{len(checks)} site(s): {failed} to fix, {warned} with warnings.")
    return 1 if failed else 0


def _safety_net(
    args: argparse.Namespace, settings: Settings, portfolio: Portfolio, store: PlanStore
) -> int:
    from vaticore.pipeline.runner import send_missing

    channels: list[Channel] = []
    for name in args.channel or []:
        if name == "console":
            channels.append(ConsoleChannel())
        elif name == "whatsapp":
            channels.append(_whatsapp(settings, "template"))
        elif name == "email":
            channels.append(_email(settings))
    if not channels:
        raise SystemExit("safety-net needs --channel console, whatsapp or email")
    acted = send_missing(
        portfolio, store, channels=channels, recipients=_recipients(args, settings)
    )
    for run in acted:
        sent = ", ".join(f"{who} {status}" for who, status in run.deliveries) or "nobody to send to"
        print(f"{run.operator_id}/{run.site_id} {run.plan_date}: {run.status}; {sent}")
    missed = [r for r in acted if r.status == "missed" and r.error]  # newly missed only
    if missed:
        alert = (
            f"The daily planning job did not run for {len(missed)} site(s); they were told "
            "'no plan today, run as usual': "
            + ", ".join(f"{r.operator_id}/{r.site_id}" for r in missed)
        )
        print(f"ALERT {alert}")
        _alert_ops(settings, [alert], subject="Vaticore: daily plans missed")
    if not acted:
        print("every site's message was already delivered")
    return 1 if missed else 0


def _weather(settings: Settings) -> OpenMeteoProvider | None:
    """Live weather, if configured and licensed for this environment."""
    if settings.weather_provider.lower() in ("", "none", "off"):
        return None
    key = settings.weather_api_key.get_secret_value() if settings.weather_api_key else None
    if key is None and settings.environment == "production":
        # Open-Meteo's free API is non-commercial: production needs a key.
        print("weather: skipped (set VATICORE_WEATHER_API_KEY for commercial use)")
        return None
    return OpenMeteoProvider(api_key=key)


def _alert_ops(settings: Settings, alerts: list[str], subject: str | None = None) -> None:
    if not settings.ops_email:
        return
    if not settings.smtp_host or not settings.email_from:
        print("alerts not emailed: VATICORE_OPS_EMAIL is set but SMTP is not")
        return
    if subject is None:
        subject = f"Vaticore model monitoring: {len(alerts)} change(s)"
        body = (
            "Model monitoring changed the planning model at these sites. Plans continue on "
            "the next model in the chain; suspended models keep running in shadow and "
            "return when their record recovers.\n\n" + "\n".join(f"- {a}" for a in alerts)
        )
    else:
        body = "\n".join(alerts) + "\n\nWhat to do: docs/runbook.md."
    result = _email(settings).send_text(settings.ops_email, subject, body)
    print(f"alerts emailed to ops: {result.status}")


def _monitor(store: PlanStore, operator_id: str, site_id: str, weeks: int) -> None:
    print(f"== {operator_id}/{site_id}")
    statuses = store.model_statuses(operator_id, site_id)
    for model, row in statuses.items():
        print(f"  {model}: {row['status']} since {row['since']} ({row['reason']})")
    report = monitoring.weekly_report(store, operator_id, site_id, weeks=weeks)
    if report.empty:
        print("  no scored days yet")
        return
    shown = report.assign(
        range_held=report["range_held"].map(lambda v: f"{v:.0%}"),
        pinball=report["pinball"].map(lambda v: f"{v:.3f}"),
        skill=report["skill"].map(lambda v: "n/a" if pd.isna(v) else f"{v:+.0%}"),
    )
    print("  " + shown.to_string(index=False).replace("\n", "\n  "))


def _apikey(args: argparse.Namespace, store: PlanStore) -> int:
    now = datetime.now(tz=UTC)
    if args.action == "create":
        if not args.operator:
            raise SystemExit("apikey create needs --operator")
        key = store.create_api_key(args.operator, args.name or "", now)
        print("New key (shown once; store it in a password manager):")
        print(key)
        return 0
    if args.action == "revoke":
        if not args.key_id:
            raise SystemExit("apikey revoke needs --key-id")
        ok = store.revoke_api_key(args.key_id, now)
        print("revoked" if ok else "no such key")
        return 0 if ok else 1
    print(store.api_keys().to_string(index=False))
    return 0


def _migrate(args: argparse.Namespace, settings: Settings) -> int:
    targets = {migrations.READINGS: settings.database_url}
    targets[migrations.PLANS] = settings.plan_store_url or settings.database_url
    # Opening each store creates its baseline tables.
    PlanStore(targets[migrations.PLANS]).close()
    get_repository(settings).close()
    for database, url in targets.items():
        if args.status:
            state = migrations.status(url, database)
            pending = ", ".join(f"{m.version} {m.name}" for m in state.pending) or "none"
            print(f"{database}: applied {list(state.applied)}; pending: {pending}")
            continue
        for m in migrations.migrate(url, database):
            print(f"{database}: applied {m.version} {m.name}")
    if not args.status:
        print("migrations: up to date")
    return 0


def _backup(args: argparse.Namespace, settings: Settings) -> int:
    urls = [settings.database_url]
    if settings.plan_store_url and settings.plan_store_url != settings.database_url:
        urls.append(settings.plan_store_url)
    ok = True
    for url in urls:
        result = backup(url, args.out)
        print(f"backed up {sum(result.tables.values())} rows to {result.path}")
        if args.verify:
            scratch = (
                settings.restore_test_url.get_secret_value() if settings.restore_test_url else None
            )
            check = verify_restore(result.path, scratch)
            print(check.to_text())
            ok = ok and check.ok
    if args.keep:
        for path in prune(args.out, args.keep):
            print(f"removed old backup {path.name}")
    return 0 if ok else 1


def _fuel_record(args: argparse.Namespace, store: PlanStore) -> int:
    now = datetime.now(tz=UTC)
    if args.action == "add":
        if not args.site or args.litres is None or not args.at:
            raise SystemExit("fuel add needs --site OPERATOR/SITE, --litres and --at")
        operator_id, site_id = _site_key(args.site[0])
        at = _when(args.at, args.timezone)
        store.add_delivery(operator_id, site_id, at, args.litres, args.reference, now)
        print(f"recorded {args.litres:,.0f} L for {operator_id}/{site_id} at {at.isoformat()}")
        return 0
    if not args.csv:
        raise SystemExit("fuel import needs --csv (operator_id, site_id, delivered_at, litres)")
    rows = pd.read_csv(args.csv)
    missing = {"operator_id", "site_id", "delivered_at", "litres"} - set(rows.columns)
    if missing:
        raise SystemExit(f"fuel import: the file has no column(s) {sorted(missing)}")
    for row in rows.to_dict("records"):
        reference = row.get("reference")
        store.add_delivery(
            str(row["operator_id"]),
            str(row["site_id"]),
            _when(str(row["delivered_at"]), args.timezone),
            float(row["litres"]),
            None if reference is None or pd.isna(reference) else str(reference),
            now,
        )
    print(f"recorded {len(rows)} deliveries")
    return 0


def _when(text: str, timezone: str | None) -> datetime:
    ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        if not timezone:
            raise SystemExit(f"{text!r} has no UTC offset: add one, or pass --timezone")
        ts = ts.tz_localize(timezone)
    return ts.tz_convert("UTC").to_pydatetime()


def _fuel_report(
    args: argparse.Namespace, portfolio: Portfolio, repo: object, store: PlanStore
) -> int:
    from vaticore.fuel import Severity, reconcile

    end = pd.Timestamp(datetime.now(tz=UTC))
    start = end - pd.Timedelta(days=args.days)
    chosen = {_site_key(s) for s in args.site} if args.site else None
    worst = 0
    for site in portfolio.sites:
        if site.generator is None or (chosen is not None and site.key not in chosen):
            continue
        readings = repo.read_history(site.operator_id, site.site_id, start, end)  # type: ignore[attr-defined]
        deliveries = store.deliveries_for(
            site.operator_id, site.site_id, start.to_pydatetime(), end.to_pydatetime()
        )
        report = reconcile(site, readings, deliveries, start=start, end=end)
        print(report.to_text(site.timezone) + "\n")
        if report.status is Severity.ALERT:
            worst = 1
    return worst


def _savings_report(
    args: argparse.Namespace,
    settings: Settings,
    portfolio: Portfolio,
    repo: object,
    store: PlanStore,
) -> int:
    from vaticore.pipeline.savings import Period, savings_report

    sites = [s for s in portfolio.sites if s.operator_id == args.operator]
    if not sites:
        raise SystemExit(f"no sites for operator {args.operator!r} in the portfolio")
    try:
        report = savings_report(
            sites,
            repo,  # type: ignore[arg-type]
            store,
            baseline=Period.parse(args.baseline),
            pilot=Period.parse(args.pilot),
            control=args.control or (),
        )
    except ValueError as exc:
        raise SystemExit(f"report: {exc}") from exc
    markdown = report.to_markdown()
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"pilot-report-{args.operator}-{report.pilot.end:%Y-%m-%d}"
        (args.out / f"{stem}.md").write_text(markdown)
        (args.out / f"{stem}.json").write_text(report.to_json())
        print(f"written to {args.out / stem}.md and .json")
    for address in args.email or []:
        result = _email(settings).send_text(
            address, f"Vaticore pilot report: {args.operator}, {report.pilot.label()}", markdown
        )
        print(f"emailed to {address}: {result.status}")
    return 0


def _shadow_review(args: argparse.Namespace, portfolio: Portfolio, store: PlanStore) -> int:
    from vaticore.pipeline.savings import Period
    from vaticore.pipeline.shadow import last_days, shadow_review

    sites = [s for s in portfolio.sites if s.operator_id == args.operator]
    if not sites:
        raise SystemExit(f"no sites for operator {args.operator!r} in the portfolio")
    period = (
        Period.parse(args.period)
        if args.period
        else last_days(args.days, datetime.now(tz=UTC).date())
    )
    review = shadow_review(sites, store, period)
    markdown = review.to_markdown()
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"shadow-review-{args.operator}-{period.end:%Y-%m-%d}"
        (args.out / f"{stem}.md").write_text(markdown)
        (args.out / f"{stem}.json").write_text(review.to_json())
        print(f"written to {args.out / stem}.md and .json")
    return 0


def _summaries(
    args: argparse.Namespace,
    settings: Settings,
    portfolio: Portfolio,
    repo: object,
    store: PlanStore,
) -> int:
    from vaticore.pipeline.savings import Period
    from vaticore.pipeline.summary import send_weekly_summaries, week_of

    if not args.channel:
        raise SystemExit("summary needs --channel console, whatsapp or email")
    week = (
        Period(args.week_start, args.week_start + timedelta(days=6))
        if args.week_start
        else week_of(datetime.now(tz=UTC).date())
    )
    sent = send_weekly_summaries(
        portfolio.sites,
        repo,  # type: ignore[arg-type]
        store,
        _recipients(args, settings),
        week=week,
        whatsapp=_whatsapp(settings, "template") if "whatsapp" in args.channel else None,
        email=_email(settings) if "email" in args.channel else None,
        console="console" in args.channel,
        now=datetime.now(tz=UTC),
        force=args.force,
        template_name=settings.whatsapp_summary_template,
    )
    for d in sent:
        print(f"{d.recipient} ({d.channel}): {d.status}" + (f", {d.error}" if d.error else ""))
    if not sent:
        print("no recipients with weekly_summary = true and consent")
    return 1 if any(d.status == "failed" for d in sent) else 0


def _replies(args: argparse.Namespace, store: PlanStore) -> int:
    rows = store.feedback(args.operator)
    if not rows.empty:
        since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=args.days)
        rows = rows[pd.to_datetime(rows["received_at"], utc=True) >= since]
    if rows.empty:
        print(f"no replies in the last {args.days} days")
        return 0
    for row in rows.to_dict("records"):
        when = pd.Timestamp(str(row["received_at"])).tz_convert("Africa/Lagos")
        site = "no plan"
        if row.get("site_id"):
            day = pd.Timestamp(str(row["plan_date"])).date()
            site = f"{row['operator_id']}/{row['site_id']} {day}"
        reason = f" ({row['reason']})" if row.get("reason") else ""
        text = str(row.get("text") or "")[:60]
        print(f"{when:%d %b %H:%M}  {site}  {row['kind']}{reason}  {text!r}")
    return 0


def _optout(args: argparse.Namespace, store: PlanStore) -> int:
    if not args.phone and not args.email:
        raise SystemExit("optout needs --phone or --email")
    who = recipient_hash(args.phone) if args.phone else email_hash(args.email)
    store.set_opt_out(who, not args.undo, "manual", datetime.now(tz=UTC))
    print("opted back in" if args.undo else "opted out: no more plans to this person")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vaticore.pipeline",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="plan every site for today and deliver")
    _portfolio_arg(run)
    run.add_argument("--recipients", type=Path, help="recipients TOML (personal data)")
    run.add_argument("--date", type=date.fromisoformat, help="plan date (default: today, local)")
    run.add_argument("--site", action="append", help="operator_id/site_id; repeatable")
    run.add_argument(
        "--channel", action="append", choices=["console", "whatsapp", "email"],
        help="deliver on this channel; repeatable (default: plan only)",
    )  # fmt: skip
    run.add_argument("--whatsapp-mode", choices=["template", "text"], default="template")
    run.add_argument("--force", action="store_true", help="re-plan and resend even if sent")
    run.add_argument("--no-weather", action="store_true", help="skip live weather")

    for name, text in (
        ("score", "score finished plan days against actual readings"),
        ("scorecard", "each site's recent track record"),
        ("health", "data health report per site"),
        ("monitor", "each model's calibration and accuracy, week by week"),
    ):
        command = sub.add_parser(name, help=text)
        _portfolio_arg(command)
        if name in ("scorecard", "health"):
            command.add_argument("--days", type=int, default=30)
        if name == "monitor":
            command.add_argument("--weeks", type=int, default=8)

    ingest = sub.add_parser("ingest", help="pull new readings from monitoring platforms")
    ingest.add_argument("--sources", type=Path, help="sources TOML (default from settings)")

    fuel = sub.add_parser("fuel", help="diesel deliveries and reconciliation")
    fuel.add_argument("action", choices=["add", "import", "report"])
    _portfolio_arg(fuel)
    fuel.add_argument("--site", action="append", help="operator_id/site_id")
    fuel.add_argument("--litres", type=float)
    fuel.add_argument("--at", help="delivery time, ISO 8601 (with offset, or --timezone)")
    fuel.add_argument("--reference", help="delivery note or invoice number")
    fuel.add_argument("--timezone", help="for times without an offset, e.g. Africa/Lagos")
    fuel.add_argument("--csv", type=Path, help="deliveries file for import")
    fuel.add_argument("--days", type=int, default=30)

    rep = sub.add_parser("report", help="pilot savings report for one operator (monthly)")
    _portfolio_arg(rep)
    rep.add_argument("--operator", required=True)
    rep.add_argument(
        "--baseline", required=True, help="days before plans were sent, FIRST:LAST (local)"
    )
    rep.add_argument("--pilot", required=True, help="pilot days, FIRST:LAST (local)")
    rep.add_argument("--control", action="append", help="site_id of a control site; repeatable")
    rep.add_argument("--out", type=Path, help="also write Markdown and JSON here")
    rep.add_argument("--email", action="append", help="email the report to this address")

    site = sub.add_parser("site", help="check a portfolio file (and each site's readiness)")
    site.add_argument("action", choices=["check"])
    _portfolio_arg(site)
    site.add_argument("--site", action="append", help="operator_id/site_id; repeatable")
    site.add_argument(
        "--with-data", action="store_true",
        help="also check readings in the database, recipients and sources",
    )  # fmt: skip
    site.add_argument("--recipients", type=Path, help="recipients TOML, for --with-data")
    site.add_argument("--sources", type=Path, help="sources TOML, for --with-data")

    net = sub.add_parser(
        "safety-net", help="after the daily job: resend failed messages, cover missed sites"
    )
    _portfolio_arg(net)
    net.add_argument("--recipients", type=Path, help="recipients TOML (personal data)")
    net.add_argument("--channel", action="append", choices=["console", "whatsapp", "email"])

    shadow = sub.add_parser("shadow-review", help="go or no-go after the shadow weeks")
    _portfolio_arg(shadow)
    shadow.add_argument("--operator", required=True)
    shadow.add_argument("--days", type=int, default=28, help="the last N full days")
    shadow.add_argument("--period", help="or exact days, FIRST:LAST (local)")
    shadow.add_argument("--out", type=Path, help="also write Markdown and JSON here")

    summ = sub.add_parser("summary", help="supervisors' weekly summary (run on Mondays)")
    _portfolio_arg(summ)
    summ.add_argument("--recipients", type=Path, help="recipients TOML (personal data)")
    summ.add_argument(
        "--channel", action="append", choices=["console", "whatsapp", "email"],
        help="send on this channel; repeatable",
    )  # fmt: skip
    summ.add_argument(
        "--week-start", type=date.fromisoformat, help="Monday of the week (default: last week)"
    )
    summ.add_argument("--force", action="store_true", help="resend even if already sent")

    key = sub.add_parser("apikey", help="operator API keys: create, list, revoke")
    key.add_argument("action", choices=["create", "list", "revoke"])
    key.add_argument("--operator")
    key.add_argument("--name")
    key.add_argument("--key-id")

    opt = sub.add_parser("optout", help="stop (or with --undo, resume) plans to a person")
    opt.add_argument("--phone")
    opt.add_argument("--email")
    opt.add_argument("--undo", action="store_true")

    rep_ = sub.add_parser("replies", help="recent replies: followed, not followed, reasons")
    rep_.add_argument("--operator")
    rep_.add_argument("--days", type=int, default=7)

    test = sub.add_parser("whatsapp-test", help="send Meta's hello_world template")
    test.add_argument("--to", required=True, help="a number allowed to receive test messages")
    mail = sub.add_parser("email-test", help="send a test email")
    mail.add_argument("--to", required=True)

    mig = sub.add_parser("migrate", help="apply pending database migrations")
    mig.add_argument("--status", action="store_true", help="list without applying")

    bak = sub.add_parser("backup", help="back up the databases, optionally proving a restore")
    bak.add_argument("--out", type=Path, required=True, help="directory for backups")
    bak.add_argument("--keep", type=int, help="keep only the newest N backups")
    bak.add_argument(
        "--verify", action="store_true", help="restore into VATICORE_RESTORE_TEST_URL and compare"
    )
    chk = sub.add_parser("restore-check", help="restore a backup into a scratch database")
    chk.add_argument("--dump", type=Path, required=True)
    chk.add_argument("--scratch", help="scratch database URL (default VATICORE_RESTORE_TEST_URL)")

    demo = sub.add_parser("demo", help="a week of the full loop on synthetic data")
    demo.add_argument("--portfolio", type=Path, default=DEFAULT_PORTFOLIO)
    demo.add_argument("--days", type=int, default=7)
    return parser


def _site_key(value: str) -> tuple[str, str]:
    operator_id, _, site_id = value.partition("/")
    if not operator_id or not site_id:
        raise SystemExit(f"--site takes operator_id/site_id, got {value!r}")
    return operator_id, site_id


def _portfolio_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--portfolio", type=Path, help="sites TOML (default from settings)")


def _portfolio(args: argparse.Namespace, settings: Settings) -> Portfolio:
    path = args.portfolio or settings.portfolio_file or DEFAULT_PORTFOLIO
    return load_portfolio(path)


def _recipients(args: argparse.Namespace, settings: Settings) -> list[Recipient]:
    path = args.recipients or settings.recipients_file
    if path is None:
        raise SystemExit(
            "delivery needs a recipients file: --recipients or VATICORE_RECIPIENTS_FILE"
        )
    return load_recipients(path)


def _whatsapp(settings: Settings, mode: str) -> WhatsAppChannel:
    if settings.whatsapp_token is None or not settings.whatsapp_phone_number_id:
        raise SystemExit(
            "WhatsApp needs VATICORE_WHATSAPP_TOKEN and VATICORE_WHATSAPP_PHONE_NUMBER_ID "
            "(see docs/whatsapp-setup.md)"
        )
    return WhatsAppChannel(
        token=settings.whatsapp_token.get_secret_value(),
        phone_number_id=settings.whatsapp_phone_number_id,
        api_version=settings.whatsapp_api_version,
        template_name=settings.whatsapp_template_name,
        template_language=settings.whatsapp_template_language,
        templates=extra_templates(settings.whatsapp_extra_templates),
        mode=mode,
    )


def extra_templates(spec: str) -> dict[str, tuple[str, str]]:
    """'pcm=vaticore_daily_plan_pcm:en,ha=vaticore_daily_plan:ha' -> {lang: (name, code)}."""
    out: dict[str, tuple[str, str]] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        lang, sep, rest = item.partition("=")
        name, colon, code = rest.partition(":")
        if not sep or not colon or not lang or not name or not code:
            raise SystemExit(
                f"VATICORE_WHATSAPP_EXTRA_TEMPLATES: {item!r} should be language=template:code"
            )
        out[lang.strip()] = (name.strip(), code.strip())
    return out


def _email(settings: Settings) -> EmailChannel:
    if not settings.smtp_host or not settings.email_from:
        raise SystemExit(
            "email needs VATICORE_SMTP_HOST and VATICORE_EMAIL_FROM (see .env.example)"
        )
    return EmailChannel(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=None
        if settings.smtp_password is None
        else settings.smtp_password.get_secret_value(),
        sender=settings.email_from,
        reply_to=settings.email_reply_to,
        use_ssl=settings.smtp_ssl,
    )


if __name__ == "__main__":
    sys.exit(main())
