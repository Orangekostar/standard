"""Shared contracts and configuration for the Technical V2 pipeline."""

from core.technical_v2.config import TechnicalV2Config
from core.technical_v2.contracts import ContractError, RunStatus

__all__ = ["ContractError", "RunStatus", "TechnicalV2Config"]

