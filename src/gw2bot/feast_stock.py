from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

LOW_STOCK_THRESHOLD = 10
LOW_STOCK_REMINDER_SECONDS = 8 * 60 * 60


@dataclass(frozen=True, slots=True)
class Feast:
    guild_storage_id: int
    name: str


@dataclass(frozen=True, slots=True)
class FeastAlert:
    guild_storage_id: int
    name: str
    count: int

    @property
    def message(self) -> str:
        return f"Guild Storage is low on **{self.name}**: {self.count} left"


TRACKED_FEASTS = (
    Feast(1078, "Bowl of Fruit Salad with Mint Garnish"),
    Feast(1089, "Cilantro and Cured Meat Flatbread"),
    Feast(1102, "Cilantro Lime Sous-Vide Steak"),
    Feast(1112, "Spherified Cilantro Oyster Soup"),
)


def get_due_low_stock_alerts(
    storage: list[dict[str, Any]],
    last_alerted_at: Mapping[int, float],
    now: float,
) -> tuple[list[FeastAlert], set[int]]:
    counts = {
        int(entry["id"]): int(entry["count"])
        for entry in storage
        if "id" in entry and "count" in entry
    }
    currently_low = {
        feast.guild_storage_id
        for feast in TRACKED_FEASTS
        if counts.get(feast.guild_storage_id, 0) <= LOW_STOCK_THRESHOLD
    }
    alerts = [
        FeastAlert(
            guild_storage_id=feast.guild_storage_id,
            name=feast.name,
            count=counts.get(feast.guild_storage_id, 0),
        )
        for feast in TRACKED_FEASTS
        if feast.guild_storage_id in currently_low
        and (
            feast.guild_storage_id not in last_alerted_at
            or now - last_alerted_at[feast.guild_storage_id]
            >= LOW_STOCK_REMINDER_SECONDS
        )
    ]
    return alerts, currently_low
