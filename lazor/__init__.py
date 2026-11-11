"""Read, simulate, solve with exact search or SMT, and export boards."""

from .bff import BFFParseError, load_bff, parse_bff
from .layout import BoardLayout, build_layout
from .models import Block, Laser, Puzzle
from .simulation import SimulationError, SimulationResult, simulate
from .search import SearchResult, solve_search
from .solver import PlanningResult, solve_constraints

__all__ = [
    "BFFParseError", "Block", "BoardLayout", "Laser", "Puzzle",
    "SimulationError", "SimulationResult", "build_layout", "load_bff",
    "parse_bff", "simulate", "PlanningResult", "solve_constraints",
    "SearchResult", "solve_search",
]

