from __future__ import annotations

from datetime import UTC, datetime, timedelta

RAFFLE_CONTRIBUTION_CHANNEL_ID = 856343628984746014
RAFFLE_CONTRIBUTION_REPORT_HOURS = 6


def raffle_contribution_report_end(now: datetime) -> datetime:
    now_utc = now.astimezone(UTC)
    return now_utc.replace(
        hour=(
            now_utc.hour // RAFFLE_CONTRIBUTION_REPORT_HOURS
        ) * RAFFLE_CONTRIBUTION_REPORT_HOURS,
        minute=0,
        second=0,
        microsecond=0,
    )


def seconds_until_raffle_contribution_report(now: datetime) -> float:
    report_end = raffle_contribution_report_end(now)
    next_report = report_end + timedelta(hours=RAFFLE_CONTRIBUTION_REPORT_HOURS)
    return (next_report - now.astimezone(UTC)).total_seconds()
