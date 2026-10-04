"""Command line for the daily pipeline.

    uv run python -m vaticore.pipeline ingest                   # pull new readings
    uv run python -m vaticore.pipeline run --channel whatsapp   # plan and send (daily)
    uv run python -m vaticore.pipeline run --channel whatsapp --channel email
    uv run python -m vaticore.pipeline score                    # score finished days
    uv run python -m vaticore.pipeline scorecard                # track record per site
    uv run python -m vaticore.pipeline monitor                  # each model, week by week
    uv run python -m vaticore.pipeline health --days 30         # data health per site
    uv run python -m vaticore.pipeline apikey create --operator example-towerco --name ops
    uv run python -m vaticore.pipeline optout --email someone@example.com
    uv run python -m vaticore.pipeline whatsapp-test --to +234...
    uv run python -m vaticore.pipeline email-test --to someone@example.com
    uv run python -m vaticore.pipeline demo                     # full loop, synthetic

Settings (portfolio, recipients, sources, database, credentials) come from the
environment or .env (see .env.example); flags override them. run and ingest
exit with status 1 if any site failed, so a scheduler can alert on it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vaticore.config import Settings, get_settings
from vaticore.delivery.channels import Channel, ConsoleChannel, EmailChannel, WhatsAppChannel
from vaticore.delivery.message import PlanMessage
from vaticore.delivery.recipients import Recipient, email_hash, load_recipients, recipient_hash
from vaticore.features.weather import OpenMeteoProvider
from vaticore.ingestion.connectors import load_sources, sync_sources
from vaticore.pipeline import monitoring
from vaticore.pipeline.health import site_health_report
from vaticore.pipeline.runner import run_portfolio
from vaticore.pipeline.scoring import score_due, scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Portfolio, load_portfolio
from vaticore.storage import get_repository

DEFAULT_PORTFOLIO = Path("examples/sites/nigeria_portfolio.toml")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

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
        if args.command == "apikey":
            return _apikey(args, store)
        if args.command == "optout":
            return _optout(args, store)

        repo = get_repository(settings)
        try:
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
            return _run(args, settings, portfolio, repo, store)
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


def _alert_ops(settings: Settings, alerts: list[str]) -> None:
    if not settings.ops_email:
        return
    if not settings.smtp_host or not settings.email_from:
        print("alerts not emailed: VATICORE_OPS_EMAIL is set but SMTP is not")
        return
    body = (
        "Model monitoring changed the planning model at these sites. Plans continue on "
        "the next model in the chain; suspended models keep running in shadow and return "
        "when their record recovers.\n\n" + "\n".join(f"- {a}" for a in alerts)
    )
    result = _email(settings).send_text(
        settings.ops_email, f"Vaticore model monitoring: {len(alerts)} change(s)", body
    )
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

    key = sub.add_parser("apikey", help="operator API keys: create, list, revoke")
    key.add_argument("action", choices=["create", "list", "revoke"])
    key.add_argument("--operator")
    key.add_argument("--name")
    key.add_argument("--key-id")

    opt = sub.add_parser("optout", help="stop (or with --undo, resume) plans to a person")
    opt.add_argument("--phone")
    opt.add_argument("--email")
    opt.add_argument("--undo", action="store_true")

    test = sub.add_parser("whatsapp-test", help="send Meta's hello_world template")
    test.add_argument("--to", required=True, help="a number allowed to receive test messages")
    mail = sub.add_parser("email-test", help="send a test email")
    mail.add_argument("--to", required=True)

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
        mode=mode,
    )


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
