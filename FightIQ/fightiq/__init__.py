"""FightIQ — FightOdds-backed MMA straight / prop / parlay helpers."""

from .client import FightOddsClient
from .bot_flow import TicketBuilder, BetMode
from .models import Event, Fight, Leg, Ticket, Selection
from .odds_math import american_to_decimal, decimal_to_american, combine_parlay
from .props_catalog import PropCatalogService, get_prop_service
from .slip_parser import parse_slip_text
from .grader import SlipGrader
from .store import TicketStore
from .espn_mma import EspnMmaClient
from .quickpick import QuickPickClient

__all__ = [
    "FightOddsClient",
    "TicketBuilder",
    "BetMode",
    "Event",
    "Fight",
    "Leg",
    "Ticket",
    "Selection",
    "american_to_decimal",
    "decimal_to_american",
    "combine_parlay",
    "PropCatalogService",
    "get_prop_service",
    "parse_slip_text",
    "SlipGrader",
    "TicketStore",
    "EspnMmaClient",
    "QuickPickClient",
]

__version__ = "0.2.0"
