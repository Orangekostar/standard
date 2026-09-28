from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.backtest.execution_v2 import SecurityRule


def security_rule(board: Any, date: str, config: dict[str, Any]) -> SecurityRule | None:
    for record in config["security_rule_versions"]:
        if (record["board"] != board or not record["verified"]
                or record["effective_from"] > date
                or (record["effective_to"] and record["effective_to"] < date)):
            continue
        return SecurityRule(
            lot_size=record["quantity_increment"],
            tick_size=Decimal(str(config["shared_portfolio"]["tick_size_cny"])),
            effective_from=record["effective_from"], effective_to=record["effective_to"],
            source=record["version"] + "|" + record["source"],
            minimum_quantity=record["minimum_quantity"],
            quantity_increment=record["quantity_increment"],
            maximum_quantity=record["maximum_quantity"],
        )
    return None
