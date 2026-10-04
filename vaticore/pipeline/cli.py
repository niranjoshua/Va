"""Command line for the daily pipeline.

    uv run python -m vaticore.pipeline run --channel whatsapp     # the daily job
    uv run python -m vaticore.pipeline score                      # score finished days
    uv run python -m vaticore.pipeline scorecard                  # track record per site
    uv run python -m vaticore.pipeline whatsapp-test --to +234... # check credentials
    uv run python -m vaticore.pipeline demo                       # full loop, synthetic

Settings (portfolio, recipients, database, WhatsApp credentials) come from the
environment or .env (see .env.example); flags override them. The run command
exits with status 1 if any site failed, so a scheduler can alert on it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from vaticore.config import Settings, get_settings
from vaticore.delivery.channels import Channel, ConsoleChannel, WhatsAppChannel
from vaticore.delivery.recipients import Recipient, load_recipients
from vaticore.pipeline.runner import run_portfolio
from vaticore.pipeline.scoring import score_due, scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Portfolio, load_portfolio
from vaticore.storage import get_repository

DEFAULT_PORTFOLIO = Path("examples/sites/nigeria_portfolio.toml")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vaticore.pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="plan every site for today and deliver")
    _common(run)
    run.add_argument("--recipients", type=Path, help="recipients TOML (phone numbers)")
    run.add_argument("--date", type=date.fromisoformat, help="plan date (default: today, local)")
    run.add_argument("--site", action="append", help="operator_id/site_id; repeatable")
    run.add_argument("--channel", choices=["none", "console", "whatsapp"], default="none")
    run.add_argument("--whatsapp-mode", choices=["template", "text"], default="template")
    run.add_argument("--force", action="store_true", help="re-plan and resend even if sent")

    score = sub.add_parser("score", help="score finished plan days against actual readings")
    _common(score)

    card = sub.add_parser("scorecard", help="each site's recent track record")
    _common(card)
    card.add_argument("--days", type=int, default=30)

    test = sub.add_parser("whatsapp-test", help="send Meta's hello_world template")
    test.add_argument("--to", required=True, help="a number allowed to receive test messages")

    demo = sub.add_parser("demo", help="a week of the full loop on synthetic data")
    demo.add_argument("--portfolio", type=Path, default=DEFAULT_PORTFOLIO)
    demo.add_argument("--days", type=int, default=7)

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

    portfolio = _portfolio(args, settings)
    repo = get_repository(settings)
    store = PlanStore(settings.plan_store_url or settings.database_url)
    try:
        if args.command == "score":
            for record in score_due(portfolio, repo, store):
                print(
                    f"{record.operator_id}/{record.site_id} {record.plan_date}: "
                    f"{record.hours_scored} h scored, plan {record.fuel_plan_l:.0f} L, "
                    f"baseline {record.fuel_baseline_l:.0f} L"
                )
            return 0
        if args.command == "scorecard":
            for site in portfolio.sites:
                print(scorecard(store, site.operator_id, site.site_id, days=args.days).summary())
            return 0

        channel: Channel | None = None
        if args.channel == "console":
            channel = ConsoleChannel()
        elif args.channel == "whatsapp":
            channel = _whatsapp(settings, args.whatsapp_mode)
        recipients = _recipients(args, settings) if channel is not None else []
        sites = [_site_key(s) for s in args.site] if args.site else None
        runs = run_portfolio(
            portfolio,
            repo,
            store,
            plan_date=args.date,
            sites=sites,
            channel=channel,
            recipients=recipients,
            force=args.force,
        )
        for r in runs:
            sent = ", ".join(f"{who} {status}" for who, status in r.deliveries) or "not sent"
            print(f"{r.operator_id}/{r.site_id} {r.plan_date}: {r.status} ({r.model}); {sent}")
            if r.error:
                print(f"  error: {r.error}")
        return 1 if any(r.status == "failed" for r in runs) else 0
    finally:
        store.close()
        repo.close()


def _site_key(value: str) -> tuple[str, str]:
    operator_id, _, site_id = value.partition("/")
    if not operator_id or not site_id:
        raise SystemExit(f"--site takes operator_id/site_id, got {value!r}")
    return operator_id, site_id


def _common(parser: argparse.ArgumentParser) -> None:
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


if __name__ == "__main__":
    sys.exit(main())
