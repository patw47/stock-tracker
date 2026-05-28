from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from agents.warren.models import MacroContext

logger = logging.getLogger(__name__)


class MacroProviderError(Exception):
    """Base error raised by MacroContextProvider implementations."""


class MacroContextProvider(ABC):
    """Async source of MacroContext snapshots.

    Implementations are expected to be safe to call from an event loop and
    to return a MacroContext with as many fields populated as the upstream
    data allows. Missing fields stay None rather than raising.
    """

    @abstractmethod
    async def fetch(self) -> MacroContext:
        """Return the latest MacroContext snapshot."""
