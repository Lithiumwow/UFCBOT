"""Grade MMA slip legs against ESPN UFC results + FightOdds outcomes."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from .client import FightOddsClient
from .espn_mma import EspnFightResult, EspnMmaClient, _name_hit
from .odds_math import american_to_decimal
from .store import TicketStore

log = logging.getLogger(__name__)


@dataclass
class GradeResult:
    status: str  # pending | won | lost | push | void
    note: str
    source: str
    detail: dict


class SlipGrader:
    def __init__(
        self,
        store: TicketStore | None = None,
        espn: EspnMmaClient | None = None,
        odds: FightOddsClient | None = None,
    ) -> None:
        self.store = store or TicketStore()
        self.espn = espn or EspnMmaClient()
        self.odds = odds or FightOddsClient()
        self._espn_cache: list[EspnFightResult] | None = None
        self._fo_cache: dict[str, dict] = {}

    def _espn_results(self, force: bool = False) -> list[EspnFightResult]:
        if self._espn_cache is None or force:
            self._espn_cache = self.espn.results_window(days_back=10, days_forward=0)
        return self._espn_cache

    def grade_all_open(self) -> list[dict]:
        out = []
        for slip in self.store.open_slips():
            out.append(self.grade_slip(slip["id"]))
        return out

    def grade_slip(self, slip_id: str) -> dict:
        slip = self.store.get_slip(slip_id)
        if not slip:
            raise LookupError(f"Slip not found: {slip_id}")

        leg_grades = []
        for leg in slip["legs"]:
            if leg["status"] in {"won", "lost", "push", "void"}:
                leg_grades.append(
                    {
                        "idx": leg["idx"],
                        "status": leg["status"],
                        "note": leg.get("grade_note") or "already graded",
                    }
                )
                continue
            gr = self.grade_leg(leg)
            self.store.update_leg_grade(
                slip_id, leg["idx"], gr.status, gr.note, gr.detail
            )
            leg_grades.append(
                {
                    "idx": leg["idx"],
                    "status": gr.status,
                    "note": gr.note,
                    "source": gr.source,
                    "detail": gr.detail,
                }
            )

        # refresh
        slip = self.store.get_slip(slip_id)
        assert slip
        summary = {
            "slip_id": slip_id,
            "status": slip["status"],
            "legs": leg_grades,
            "payout_units": self._payout_units(slip),
        }
        self.store.set_slip_grade(slip_id, slip["status"], summary)
        return summary

    def grade_leg(self, leg: dict) -> GradeResult:
        # Prefer ESPN for finished UFC bouts; FightOdds only as fallback
        result_espn = self._match_espn(leg)
        result_fo = None
        if not result_espn or not result_espn.get("completed"):
            result_fo = self._match_fightodds(leg)

        result = result_espn if (result_espn and result_espn.get("completed")) else (result_fo or result_espn)
        source = (
            "espn"
            if result and result is result_espn
            else ("fightodds" if result is result_fo else "none")
        )

        if not result or not result.get("completed"):
            return GradeResult(
                status="pending",
                note="Fight not final yet (or not found on ESPN/FightOdds)",
                source=source,
                detail={"espn": result_espn, "fightodds": result_fo},
            )

        market = (leg.get("market") or "other").lower()
        fighter = leg.get("fighter") or ""
        winner = result.get("winner") or ""
        method = (result.get("method") or "").lower()
        rnd = result.get("round")
        sel_blob = " ".join(
            str(leg.get(k) or "") for k in ("selection", "label", "raw", "description")
        )
        if market in {"ml", "moneyline", "straight"}:
            m_round = re.search(
                r"(?:to\s+win|wins?)\s+(?:in\s+)?(?:the\s+)?(?:round|rd\.?)\s*([1-5])",
                sel_blob,
                re.I,
            )
            if m_round:
                market = f"r{m_round.group(1)}"

        # ---- moneyline ----
        if market in {"ml", "moneyline", "straight"}:
            if not fighter or not winner:
                return GradeResult(
                    "pending", "Missing fighter/winner for ML grade", source, result
                )
            hit = _name_hit(fighter, winner)
            return GradeResult(
                "won" if hit else "lost",
                f"Winner: {winner}" + ("" if hit else f" (picked {fighter})"),
                source,
                result,
            )

        # ---- fighter method ----
        if market in {"sub", "ko", "dec"}:
            if not fighter or not winner:
                return GradeResult("pending", "Missing fighter/winner", source, result)
            if not _name_hit(fighter, winner):
                return GradeResult(
                    "lost",
                    f"{fighter} did not win (winner {winner})",
                    source,
                    result,
                )
            need = {"sub": "submission", "ko": "ko", "dec": "decision"}[market]
            if not method:
                return GradeResult(
                    "pending",
                    f"{fighter} won but method unknown",
                    source,
                    result,
                )
            ok = method == need or (need == "ko" and method in {"ko", "tko"})
            return GradeResult(
                "won" if ok else "lost",
                f"Won by {method} (needed {need})",
                source,
                result,
            )

            # ---- round props ----
        m = re.match(r"^(sub|ko|dec)_r([1-5])$", market)
        if m:
            method_need, want_round_s = m.group(1), m.group(2)
            want_round = int(want_round_s)
            if not fighter or not winner:
                return GradeResult("pending", "Missing fighter/winner", source, result)
            if not _name_hit(fighter, winner):
                return GradeResult("lost", f"{fighter} did not win", source, result)
            if rnd is None:
                return GradeResult(
                    "pending", "Winner found but round unknown", source, result
                )
            if rnd != want_round:
                return GradeResult(
                    "lost", f"Finished R{rnd}, needed R{want_round}", source, result
                )
            need = {"sub": "submission", "ko": "ko", "dec": "decision"}[method_need]
            ok = method == need or (need == "ko" and method in {"ko", "tko"})
            return GradeResult(
                "won" if ok else "lost",
                f"R{rnd} by {method} (needed {need})",
                source,
                result,
            )

        m = re.match(r"^r([1-5])$", market)
        if m:
            want_round = int(m.group(1))
            if not fighter or not winner:
                return GradeResult("pending", "Missing fighter/winner", source, result)
            if not _name_hit(fighter, winner):
                return GradeResult("lost", f"{fighter} did not win", source, result)
            if method in {"decision", "dec", "unanimous", "split"}:
                return GradeResult(
                    "lost",
                    f"Went to decision (needed a finish in round {want_round})",
                    source,
                    result,
                )
            if rnd is None:
                return GradeResult(
                    "pending", "Winner found but round unknown", source, result
                )
            if rnd != want_round:
                return GradeResult(
                    "lost", f"Finished R{rnd}, needed R{want_round}", source, result
                )
            return GradeResult("won", f"Won in round {rnd}", source, result)

        # ---- distance ----
        if market == "distance":
            itd = result.get("ends_inside")
            if itd is None:
                return GradeResult("pending", "Distance result unknown", source, result)
            side = (leg.get("side") or "").lower()
            # side yes = goes distance; no = ends inside
            if side in {"yes", "distance", "go"}:
                won = not itd
                return GradeResult(
                    "won" if won else "lost",
                    "Fight " + ("went distance" if not itd else "ended inside"),
                    source,
                    result,
                )
            if side in {"no", "inside", "itd"}:
                won = itd
                return GradeResult(
                    "won" if won else "lost",
                    "Fight " + ("ended inside" if itd else "went distance"),
                    source,
                    result,
                )
            # infer from selection text
            sel = (leg.get("selection") or leg.get("label") or "").lower()
            if "inside" in sel:
                won = itd
            else:
                won = not itd
            return GradeResult(
                "won" if won else "lost",
                "Fight " + ("ended inside distance" if itd else "went the distance"),
                source,
                result,
            )

        # ---- totals / rounds O-U ----
        if market in {"totals", "over", "under"} or leg.get("line") is not None:
            line = leg.get("line")
            side = (leg.get("side") or market or "").lower()
            if "over" in side:
                side = "over"
            elif "under" in side:
                side = "under"
            if line is None or rnd is None:
                # try parse from selection
                m = re.search(r"(\d+(?:\.\d+)?)", leg.get("selection") or "")
                if m:
                    line = float(m.group(1))
            if line is None or rnd is None or method is None:
                return GradeResult(
                    "pending",
                    "Need final round + method for totals grade",
                    source,
                    result,
                )
            # Approx total rounds completed: if ends inside round R, total ~ R-0.5
            # if decision after scheduled, total ~ periods
            if method == "decision":
                total = float(rnd)  # fight completed all of rnd
            else:
                total = float(rnd) - 0.5
            if side == "over":
                won = total > float(line)
            else:
                won = total < float(line)
            return GradeResult(
                "won" if won else "lost",
                f"Total rounds ~{total} vs {side} {line} (end R{rnd} {method})",
                source,
                result,
            )

        # fight-level method
        if market == "method_fight":
            sel = (leg.get("selection") or leg.get("label") or "").lower()
            if not method:
                return GradeResult("pending", "Method unknown", source, result)
            if "submission" in sel:
                need = "submission"
            elif "ko" in sel or "tko" in sel:
                need = "ko"
            elif "decision" in sel:
                need = "decision"
            elif "draw" in sel:
                return GradeResult(
                    "lost" if winner else "pending",
                    f"Winner={winner}",
                    source,
                    result,
                )
            else:
                return GradeResult(
                    "pending",
                    f"Unmapped fight method prop: {sel}",
                    source,
                    result,
                )
            won = method == need or (need == "ko" and method in {"ko", "tko"})
            # handle "doesn't end by X"
            if "doesn't" in sel or "does not" in sel or "doesn't" in sel:
                won = not won
            return GradeResult(
                "won" if won else "lost",
                f"Fight method={method}",
                source,
                result,
            )

        return GradeResult(
            "pending",
            f"No autograde rule for market={market} yet",
            source,
            result,
        )

    def _match_espn(self, leg: dict) -> dict | None:
        fighter = leg.get("fighter") or ""
        opponent = leg.get("opponent")
        # also scrape names from label if fighter empty
        if not fighter:
            fighter = _guess_fighter(leg)
        if not fighter:
            return None
        # search cache for matching bout
        hits = []
        for fr in self._espn_results():
            if not fr.completed:
                continue
            if _name_hit(fighter, fr.fighter1) or _name_hit(fighter, fr.fighter2):
                if opponent:
                    if not (
                        _name_hit(opponent, fr.fighter1)
                        or _name_hit(opponent, fr.fighter2)
                    ):
                        continue
                hits.append(fr)
        if not hits:
            # try direct find including incomplete for pending
            fr = self.espn.find_fight(fighter, opponent)
            if not fr:
                return None
            return self._espn_dict(fr)
        # newest completed
        hits.sort(key=lambda x: x.event_date or "", reverse=True)
        return self._espn_dict(hits[0])

    def _espn_dict(self, fr: EspnFightResult) -> dict:
        return {
            "completed": fr.completed,
            "winner": fr.winner,
            "method": fr.method,
            "round": fr.round,
            "time": fr.time,
            "fighter1": fr.fighter1,
            "fighter2": fr.fighter2,
            "event": fr.event_name,
            "event_date": fr.event_date,
            "ends_inside": fr.ends_inside_distance(),
            "source": "espn",
            "competition_id": fr.competition_id,
        }

    def _match_fightodds(self, leg: dict) -> dict | None:
        fighter = leg.get("fighter") or _guess_fighter(leg)
        if not fighter:
            return None
        last = fighter.strip().split()[-1]
        try:
            fights = self.odds.fights_for_fighter_lastname(last)
        except Exception as e:
            log.warning("FightOdds lookup failed: %s", e)
            return None
        for f in fights:
            # refresh detail
            if f.slug in self._fo_cache:
                node = self._fo_cache[f.slug]
            else:
                try:
                    data = self.odds.gql(
                        """
                        query($s: String!) {
                          fightBySlug(slug: $s) {
                            slug
                            fighter1 { firstName lastName }
                            fighter2 { firstName lastName }
                            fighterWinner { firstName lastName }
                            methodOfVictory1 methodOfVictory2
                            round duration
                            event { name date }
                          }
                        }
                        """,
                        {"s": f.slug},
                    )
                    node = data.get("fightBySlug") or {}
                    self._fo_cache[f.slug] = node
                except Exception:
                    continue
            if not node:
                continue
            f1 = _nm(node.get("fighter1"))
            f2 = _nm(node.get("fighter2"))
            if not (_name_hit(fighter, f1) or _name_hit(fighter, f2)):
                continue
            winner = _nm(node.get("fighterWinner")) if node.get("fighterWinner") else None
            if not winner:
                continue  # not final
            method = _fo_method(node.get("methodOfVictory1"), node.get("methodOfVictory2"))
            return {
                "completed": True,
                "winner": winner,
                "method": method,
                "round": node.get("round"),
                "time": node.get("duration"),
                "fighter1": f1,
                "fighter2": f2,
                "event": (node.get("event") or {}).get("name"),
                "event_date": (node.get("event") or {}).get("date"),
                "ends_inside": method in {"ko", "submission"} if method else None,
                "source": "fightodds",
                "slug": node.get("slug"),
            }
        return None

    def _payout_units(self, slip: dict) -> float | None:
        """1u stake → profit units for settled tickets."""
        legs = slip.get("legs") or []
        if not legs:
            return None
        if any(l["status"] == "pending" for l in legs):
            return None
        if any(l["status"] == "lost" for l in legs):
            return -1.0
        # all won/push/void
        dec = 1.0
        active = 0
        for l in legs:
            if l["status"] in {"push", "void"}:
                continue
            if l["status"] != "won":
                return None
            if l.get("american") is None:
                return None
            dec *= float(american_to_decimal(int(l["american"])))
            active += 1
        if active == 0:
            return 0.0
        return round(dec - 1.0, 4)


def _nm(node: dict | None) -> str:
    if not node:
        return ""
    return f"{node.get('firstName', '')} {node.get('lastName', '')}".strip()


def _fo_method(m1: str | None, m2: str | None) -> str | None:
    blob = f"{m1 or ''} {m2 or ''}".lower()
    if "sub" in blob:
        return "submission"
    if "ko" in blob or "tko" in blob:
        return "ko"
    if "dec" in blob or "unanimous" in blob or "split" in blob or "majority" in blob:
        return "decision"
    return None


def _guess_fighter(leg: dict) -> str:
    for key in ("selection", "label", "raw"):
        text = leg.get(key) or ""
        m = re.match(
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-z'.-]+){0,2})",
            text.strip(),
        )
        if m:
            return m.group(1)
    return ""
