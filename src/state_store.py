"""
Session State Persistence

Saves and restores bot session state (Portfolio, OrderManager, Strategy ladders)
so the bot can be stopped and resumed without losing track of:
  - placed orders and their parent ladder/child links
  - filled positions and the trades_history that drives realized P&L
  - distribution-mode pending child queue
  - sequential-mode placement progress
  - per-ladder status (pending/active/closed) and child counts
"""
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON-serializable")


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _normalize_level(level):
    """Portfolio uses an int level for plain ladders and a (level, child_idx)
    tuple for distribution children. JSON has no tuple type, so on load we
    convert lists of length 2 back to tuples to keep equality checks working."""
    if isinstance(level, list) and len(level) == 2:
        return (level[0], level[1])
    return level


class StateStore:
    """Atomic JSON-backed persistence for the trading session."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self):
        return self.path.is_file()

    def reset(self):
        """Move any existing state file aside so the next run starts fresh.
        Returns the backup path if one was created, else None."""
        if not self.path.exists():
            return None
        backup = self.path.with_name(self.path.name + ".reset." + datetime.now().strftime("%Y%m%d_%H%M%S"))
        shutil.move(str(self.path), str(backup))
        logger.info(f"State reset: moved {self.path} -> {backup}")
        return backup

    def save(self, portfolio, order_manager, strategies):
        """Atomically write the current session state to disk."""
        data = {
            "version": STATE_VERSION,
            "saved_at": _iso_now(),
            "portfolio": self._serialize_portfolio(portfolio),
            "strategies": {
                s.config["name"]: self._serialize_strategy(s) for s in strategies
            },
            "order_manager": self._serialize_order_manager(order_manager),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=_json_default)
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                os.replace(str(self.path), str(backup))
            except OSError:
                pass
        os.replace(str(tmp), str(self.path))

    def load(self):
        """Load and return the raw state dict (no application yet)."""
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != STATE_VERSION:
            logger.warning(f"State file version {data.get('version')} != {STATE_VERSION}, "
                           f"attempting to load anyway")
        return data

    def apply(self, data, portfolio, order_manager, strategies):
        """Mutate portfolio/order_manager/strategies in place from saved data.

        Strategies must already be constructed (their config drives ladder
        structure). This call replaces ladder values with saved ones and
        rebuilds parent_ladder back-references on children.
        """
        self._apply_portfolio(data.get("portfolio", {}), portfolio)

        strategies_by_name = {s.config["name"]: s for s in strategies}
        for name, sdata in data.get("strategies", {}).items():
            strat = strategies_by_name.get(name)
            if strat is None:
                logger.warning(f"State has strategy '{name}' but it isn't loaded; skipping")
                continue
            self._apply_strategy(sdata, strat)

        self._apply_order_manager(data.get("order_manager", {}), order_manager, strategies_by_name)

    # ---- serialize ----------------------------------------------------------

    def _serialize_portfolio(self, p):
        positions = {}
        for name, data in p.positions.items():
            positions[name] = {
                "total_cost": data.get("total_cost", 0.0),
                "total_quantity": data.get("total_quantity", 0.0),
                "ladders": [self._serialize_position(pos) for pos in data.get("ladders", [])],
            }
        return {
            "initial_capital": p.initial_capital,
            "capital_allocated": p.capital_allocated,
            "capital_free": p.capital_free,
            "positions": positions,
            "trades_history": [self._serialize_trade(t) for t in p.trades_history],
        }

    def _serialize_position(self, pos):
        out = dict(pos)
        if isinstance(out.get("level"), tuple):
            out["level"] = list(out["level"])
        for k in ("timestamp", "close_timestamp"):
            if k in out and isinstance(out[k], datetime):
                out[k] = out[k].isoformat()
        return out

    def _serialize_trade(self, t):
        out = dict(t)
        if isinstance(out.get("level"), tuple):
            out["level"] = list(out["level"])
        if isinstance(out.get("timestamp"), datetime):
            out["timestamp"] = out["timestamp"].isoformat()
        return out

    def _serialize_strategy(self, strat):
        return {
            "ladders": [self._serialize_ladder(l) for l in strat.ladders],
        }

    def _serialize_ladder(self, ladder):
        # Drop any 'parent_ladder' on children to avoid recursion; we'll restore
        # the link by parent_level on load.
        out = {}
        for k, v in ladder.items():
            if k == "children":
                continue  # handled below
            out[k] = v
        children = ladder.get("children")
        if children is not None:
            out["children"] = [self._serialize_child(c) for c in children]
        return out

    def _serialize_child(self, child):
        out = {
            k: v for k, v in child.items()
            if k != "parent_ladder"
        }
        return out

    def _serialize_order_manager(self, om):
        active_orders = {}
        for order_id, od in om.active_orders.items():
            child = od.get("child")
            ladder = od.get("ladder") or {}
            entry = {
                "strategy": od["strategy"],
                "level": od["level"],
                "type": od["type"],
                "order": od["order"],
                "ladder_level": ladder.get("level"),
                # Embed the full child dict so we can fully restore the link
                # without needing the ladder to maintain a children list.
                "child": self._serialize_child(child) if child is not None else None,
            }
            # Recovery-merged SELLs reference a list of pooled children rather
            # than a single child. Persist it so the lot can be reconstructed.
            if od.get("type") == "SELL_MERGED":
                entry["recovery_children"] = [
                    self._serialize_child(c) for c in (od.get("recovery_children") or [])
                ]
            active_orders[str(order_id)] = entry

        # _pending_children: drop parent_ladder ref (re-link on load via parent_level)
        pending = {}
        for name, queue in om._pending_children.items():
            pending[name] = [self._serialize_child(c) for c in queue]

        # _recovery_lots: drop parent_ladder ref (re-link on load via parent_level)
        recovery_lots = {}
        for name, lot in getattr(om, "_recovery_lots", {}).items():
            recovery_lots[name] = {
                "children": [self._serialize_child(c) for c in lot.get("children", [])],
                "merged_sell_order_id": lot.get("merged_sell_order_id"),
                "merged_sell_price": lot.get("merged_sell_price"),
                "merged_sell_qty": lot.get("merged_sell_qty"),
            }

        return {
            "active_orders": active_orders,
            "sequential_state": dict(om._sequential_state),
            "pending_children": pending,
            "recovery_lots": recovery_lots,
            "last_stale_check": dict(getattr(om, "_last_stale_check", {})),
        }

    # ---- apply --------------------------------------------------------------

    def _apply_portfolio(self, data, p):
        if not data:
            return
        p.initial_capital = data.get("initial_capital", p.initial_capital)
        p.capital_allocated = data.get("capital_allocated", p.capital_allocated)
        p.capital_free = data.get("capital_free", p.capital_free)
        p.positions = {}
        for name, sdata in data.get("positions", {}).items():
            ladders = []
            for pos in sdata.get("ladders", []):
                pos = dict(pos)
                pos["level"] = _normalize_level(pos.get("level"))
                pos["timestamp"] = _parse_dt(pos.get("timestamp")) or datetime.now()
                if "close_timestamp" in pos:
                    pos["close_timestamp"] = _parse_dt(pos.get("close_timestamp"))
                ladders.append(pos)
            p.positions[name] = {
                "ladders": ladders,
                "total_cost": sdata.get("total_cost", 0.0),
                "total_quantity": sdata.get("total_quantity", 0.0),
            }
        p.trades_history = []
        for t in data.get("trades_history", []):
            t = dict(t)
            t["level"] = _normalize_level(t.get("level"))
            t["timestamp"] = _parse_dt(t.get("timestamp")) or datetime.now()
            p.trades_history.append(t)

    def _apply_strategy(self, data, strat):
        saved = data.get("ladders") or []
        if not saved:
            return
        # Build new ladders list, copying values verbatim. Children are
        # attached and back-linked to their parent_ladder.
        new_ladders = []
        for l in saved:
            ladder = dict(l)
            children = ladder.pop("children", None)
            if children is not None:
                rebuilt = []
                for c in children:
                    cd = dict(c)
                    cd["parent_ladder"] = ladder
                    rebuilt.append(cd)
                ladder["children"] = rebuilt
            new_ladders.append(ladder)
        strat.ladders = new_ladders

    def _apply_order_manager(self, data, om, strategies_by_name):
        if not data:
            return

        # Build a ladder lookup per strategy: (strategy_name, level) -> ladder dict
        ladder_index = {}
        for name, strat in strategies_by_name.items():
            for ladder in strat.ladders:
                ladder_index[(name, ladder["level"])] = ladder

        # Active orders: re-link ladder by (strategy, level); rebuild child dict
        # from embedded data and re-link its parent_ladder.
        om.active_orders = {}
        for order_id_str, od in data.get("active_orders", {}).items():
            try:
                order_id = int(order_id_str)
            except (TypeError, ValueError):
                order_id = order_id_str

            otype = od.get("type")
            # SELL_MERGED has no single ladder; restore from embedded child snapshots.
            if otype == "SELL_MERGED":
                rebuilt_children = []
                for c in od.get("recovery_children", []) or []:
                    cd = dict(c)
                    parent = ladder_index.get((od["strategy"], cd.get("parent_level")))
                    if parent is not None:
                        cd["parent_ladder"] = parent
                    rebuilt_children.append(cd)
                om.active_orders[order_id] = {
                    "strategy": od["strategy"],
                    "level": od.get("level", "recovery"),
                    "type": otype,
                    "order": od["order"],
                    "recovery_children": rebuilt_children,
                }
                continue

            ladder = ladder_index.get((od["strategy"], od.get("ladder_level")))
            if ladder is None:
                logger.warning(f"Saved order {order_id} references unknown ladder "
                               f"{od.get('ladder_level')} on {od['strategy']}, dropping")
                continue
            child_data = od.get("child")
            child = None
            if child_data is not None:
                child = dict(child_data)
                child["parent_ladder"] = ladder
            om.active_orders[order_id] = {
                "strategy": od["strategy"],
                "level": od["level"],
                "type": otype,
                "order": od["order"],
                "ladder": ladder,
                "child": child,
            }

        # Sequential state (plain dict round-trip)
        seq = data.get("sequential_state", {}) or {}
        om._sequential_state = {k: dict(v) for k, v in seq.items()}

        # Pending children — re-link parent_ladder by (strategy, parent_level)
        om._pending_children = {}
        for name, queue in data.get("pending_children", {}).items():
            rebuilt = []
            for c in queue:
                cd = dict(c)
                parent = ladder_index.get((name, cd.get("parent_level")))
                if parent is None:
                    logger.warning(f"Pending child on {name} L{cd.get('parent_level')} "
                                   f"has no matching ladder; dropping")
                    continue
                cd["parent_ladder"] = parent
                rebuilt.append(cd)
            om._pending_children[name] = rebuilt

        # Recovery lots — same re-link pattern. Missing on older state files;
        # default to empty so resume still works for pre-recovery sessions.
        om._recovery_lots = {}
        for name, lot in (data.get("recovery_lots", {}) or {}).items():
            rebuilt_children = []
            for c in lot.get("children", []) or []:
                cd = dict(c)
                parent = ladder_index.get((name, cd.get("parent_level")))
                if parent is not None:
                    cd["parent_ladder"] = parent
                rebuilt_children.append(cd)
            om._recovery_lots[name] = {
                "children": rebuilt_children,
                "merged_sell_order_id": lot.get("merged_sell_order_id"),
                "merged_sell_price": lot.get("merged_sell_price"),
                "merged_sell_qty": lot.get("merged_sell_qty"),
            }
        om._last_stale_check = dict(data.get("last_stale_check", {}) or {})
