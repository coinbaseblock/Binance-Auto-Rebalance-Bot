"""
Strategy Configuration and Ladder Calculation
"""
import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)


def _generate_fibonacci(n):
    """Return the first n Fibonacci numbers starting [1, 1, 2, 3, 5, ...]."""
    if n <= 0:
        return []
    if n == 1:
        return [1]
    fib = [1, 1]
    while len(fib) < n:
        fib.append(fib[-1] + fib[-2])
    return fib[:n]


class Strategy:
    def __init__(self, config_path):
        self.config = self._load_config(config_path)
        self.ladders = []
        self._calculate_ladders()

    def _load_config(self, config_path):
        """Load strategy configuration from JSON"""
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger.info(f"Loaded strategy: {config['name']}")
        return config

    def _calculate_ladders(self):
        """Calculate ladder levels using compound multiplicative gaps.

        Instead of additive gaps (which can exceed 100% and produce negative prices),
        each level's gap is applied to the remaining price:
            buy_multiplier = (1 - gap₁) × (1 - gap₂) × (1 - gap₃) × ...

        This guarantees buy_multiplier stays positive (never reaches zero).
        """
        ladder_config = self.config['ladder_config']
        base_gap = ladder_config['base_gap']
        fibonacci = ladder_config['fibonacci']
        num_ladders = ladder_config['ladders']

        buy_multiplier = 1.0
        self.ladders = []

        gap_max = ladder_config.get('gap_max', 0.95)

        for i in range(num_ladders):
            fib = fibonacci[i]
            raw_gap = base_gap * fib
            gap = min(raw_gap, gap_max)  # Clamp: never exceed gap_max (default 95%)

            prev_multiplier = buy_multiplier
            buy_multiplier *= (1 - gap)  # Compound: multiply remaining price

            # Sell price multiplier is the previous level's buy multiplier
            sell_multiplier = prev_multiplier if i > 0 else 1.0

            ladder = {
                'level': -(i + 1),
                'fibonacci': fib,
                'raw_gap_percent': raw_gap,
                'gap_percent': gap,  # Effective gap after clamp
                'cumulative_gap_percent': 1 - buy_multiplier,  # Total drop from starting price
                'buy_price_multiplier': buy_multiplier,
                'sell_price_multiplier': sell_multiplier,
                'units': 2 ** i,  # Martingale: 1, 2, 4, 8, 16, 32...
                'status': 'pending'
            }

            self.ladders.append(ladder)

        logger.info(f"Calculated {len(self.ladders)} ladders with total swing: "
                    f"{self.ladders[-1]['cumulative_gap_percent']:.2%}" if self.ladders else "No ladders calculated")

    def update_prices(self, current_price):
        """Update ladder prices based on current market price"""
        # Calculate base USDT per unit at level -1 for true Martingale
        first_ladder_buy_price = current_price * self.ladders[0]['buy_price_multiplier']
        unit_size_key = f"unit_size_{self.config['pair'][:3].lower()}"
        unit_size = self.config['ladder_config'].get(unit_size_key, 0.01)
        base_usdt_per_unit = first_ladder_buy_price * unit_size

        for ladder in self.ladders:
            ladder['buy_price'] = current_price * ladder['buy_price_multiplier']
            ladder['sell_price'] = current_price * ladder['sell_price_multiplier']

            if ladder['buy_price'] <= 0 or ladder['sell_price'] <= 0:
                logger.error(f"Invalid price at level {ladder['level']}: "
                           f"buy=${ladder['buy_price']:.2f}, sell=${ladder['sell_price']:.2f}. Skipping.")
                continue

            # True Martingale: USDT cost doubles each level (units × base_usdt_per_unit)
            ladder['usdt_cost'] = ladder['units'] * base_usdt_per_unit
            # Calculate BTC amount based on USDT cost and buy price
            ladder['btc_amount'] = ladder['usdt_cost'] / ladder['buy_price']

    def get_active_ladders(self):
        """Get ladders that are currently active (bought but not sold)"""
        return [l for l in self.ladders if l['status'] == 'active']

    def get_pending_ladders(self):
        """Get ladders that are waiting to be triggered"""
        return [l for l in self.ladders if l['status'] == 'pending']

    def all_ladders_closed(self):
        """Check if all ladders have completed their cycle (all closed)"""
        return all(l['status'] == 'closed' for l in self.ladders if l['status'] != 'pending') and \
               any(l['status'] == 'closed' for l in self.ladders)

    def reset_ladders(self):
        """Reset all closed ladders back to pending for a new cycle"""
        reset_count = 0
        for ladder in self.ladders:
            if ladder['status'] == 'closed':
                ladder['status'] = 'pending'
                reset_count += 1
        logger.info(f"Reset {reset_count} ladders to pending for new cycle")

    def calculate_required_capital(self):
        """Calculate total capital required if all ladders are triggered"""
        return sum(ladder['usdt_cost'] for ladder in self.ladders)

    def get_distribution_config(self):
        """Return distribution-mode config with defaults applied.

        spread_mode controls how child buy prices are spaced across a ladder:
          - "geometric" (default): equal percentage gap between adjacent children
            (evenly distributed in log space — recommended)
          - "linear": equal absolute USDT gap between adjacent children
          - "fibonacci": legacy log-Fibonacci weighting (causes top-cluster)

        child_sell_mode controls each child's take-profit price:
          - "min_of_both" (default): min(child_buy * (1 + child_profit_percent),
            parent_ladder.sell_price). Closes small lots quickly on small
            bounces while still respecting the planned ladder exit.
          - "ladder": every child shares parent_ladder.sell_price (legacy).
          - "child_only": each child uses its own buy_price * (1 + profit).
        """
        placement = self.config.get('order_placement', {})
        return {
            'child_order_usdt': placement.get('child_order_usdt', 20.0),
            'proximity_percent': placement.get('proximity_percent', 0.02),
            'max_open_orders_cap': placement.get('max_open_orders_cap', 180),
            'min_children_per_ladder': placement.get('min_children_per_ladder', 2),
            'max_children_per_ladder': placement.get('max_children_per_ladder', 15),
            'spread_mode': placement.get('spread_mode', 'geometric'),
            'child_profit_percent': placement.get('child_profit_percent', 0.012),
            'child_sell_mode': placement.get('child_sell_mode', 'min_of_both'),
            # Multiplier applied to the symbol's MIN_NOTIONAL when sizing
            # children, so the rounded order still clears the filter after
            # qty/price step-rounding. 1.10 = 10% margin.
            'notional_safety_buffer': placement.get('notional_safety_buffer', 1.10),
        }

    def is_distribution_mode(self):
        """Check whether this strategy uses distribution order placement."""
        return self.config.get('order_placement', {}).get('mode') == 'distribution'

    def get_recovery_config(self):
        """Return recovery-mode config with defaults applied.

        Recovery mode coexists with normal child SELLs:
          - Normal flow: small lots sell at child_profit_percent (e.g. 1.2%)
            for steady profits in sideway markets.
          - Recovery flow: when a child fills with the price already well
            below it (drawdown_threshold), or an existing SELL has drifted
            far above the market (stale_sell_threshold), the position is
            pulled into a per-strategy "recovery lot". Once min_merge_count
            children are pooled, they are sold together as one merged SELL
            at avg_cost * (1 + profit_target). This lets the bot wait for a
            small bounce on the aggregate cost basis instead of trying to
            fill each tiny SELL at its individual deep-water target.

        Keys:
          - enabled: master switch (default False — opt-in per strategy)
          - drawdown_threshold: at-fill trigger. If
            (child_buy - current_price) / child_buy >= this, the child
            joins the recovery lot instead of getting a normal SELL.
          - stale_sell_threshold: ongoing trigger for already-placed SELLs.
            If (sell_price - current_price) / current_price >= this, the
            SELL is cancelled and its position joins the recovery lot.
          - stale_check_interval_seconds: minimum gap between stale scans.
          - profit_target: merged-lot exit margin over avg cost basis.
          - min_merge_count: only place a merged SELL once this many
            children are pooled. With a single recovery child we still
            place its individual SELL — but at the recovery profit_target,
            not the larger child_profit_percent.
        """
        recovery = self.config.get('recovery', {}) or {}
        return {
            'enabled': bool(recovery.get('enabled', False)),
            'drawdown_threshold': float(recovery.get('drawdown_threshold', 0.03)),
            'stale_sell_threshold': float(recovery.get('stale_sell_threshold', 0.05)),
            'stale_check_interval_seconds': int(recovery.get('stale_check_interval_seconds', 300)),
            'profit_target': float(recovery.get('profit_target', 0.005)),
            'min_merge_count': max(1, int(recovery.get('min_merge_count', 2))),
        }

    def calculate_child_orders(self, ladder, next_ladder_buy_price=None,
                               min_notional=0.0):
        """Split a ladder into N child orders.

        Three knobs control the shape (see get_distribution_config):
          - spread_mode: how child buy prices are spaced across the level's
            range. Default "geometric" gives evenly-spaced children (equal %
            gap), avoiding the top-cluster of the legacy Fibonacci weighting.
          - Sizing: Fibonacci-weighted USDT per child (deeper children carry
            more) summing exactly to the parent ladder's usdt_cost.
          - child_sell_mode: each child's take-profit. Default "min_of_both"
            uses the closer of (child_buy × (1 + child_profit_percent)) and
            the parent ladder's planned sell. This lets small lots close on
            small bounces while preserving the planned ladder exit.

        The child price range is [next_ladder_buy_price, ladder['buy_price']].
        For the deepest ladder (no next), we extrapolate using the ladder's own
        gap_percent so the span matches the existing cadence.

        Args:
            ladder: dict with buy_price, sell_price, usdt_cost, level, gap_percent
            next_ladder_buy_price: buy_price of the ladder one level deeper, or
                None if this is the deepest ladder.
            min_notional: symbol's MIN_NOTIONAL filter value. When > 0, the
                child count is reduced so even the smallest Fibonacci-weighted
                child clears min_notional × notional_safety_buffer. Without
                this clamp, fib weights like 1/33 produce sub-$5 top children
                that Binance rejects with "Filter failure: NOTIONAL".

        Returns:
            list of child dicts: {idx, parent_level, buy_price, sell_price,
                usdt_cost, qty, status}
        """
        cfg = self.get_distribution_config()
        target_size = cfg['child_order_usdt']
        min_n = max(1, int(cfg['min_children_per_ladder']))
        max_n = max(min_n, int(cfg['max_children_per_ladder']))
        spread_mode = cfg['spread_mode']
        child_profit = max(0.0, float(cfg['child_profit_percent']))
        child_sell_mode = cfg['child_sell_mode']
        notional_floor = max(0.0, float(min_notional)) * float(
            cfg.get('notional_safety_buffer', 1.10)
        )

        top_price = ladder['buy_price']
        total_usdt = ladder['usdt_cost']
        ladder_sell = ladder['sell_price']

        if top_price <= 0 or total_usdt <= 0:
            return []

        def child_sell_for(buy_price):
            """Resolve a child's sell price under the active child_sell_mode."""
            if child_sell_mode == 'ladder':
                return ladder_sell
            if child_sell_mode == 'child_only':
                return buy_price * (1.0 + child_profit)
            # default: 'min_of_both' — close small lots fast on tiny bounces
            # but never above the planned ladder exit.
            return min(buy_price * (1.0 + child_profit), ladder_sell)

        # Lower bound of the child range
        if next_ladder_buy_price is not None and next_ladder_buy_price > 0:
            bottom_price = next_ladder_buy_price
        else:
            # Deepest ladder: extrapolate using its own gap_percent
            gap = ladder.get('gap_percent', 0.02)
            bottom_price = top_price * (1 - max(gap, 0.005))

        if bottom_price >= top_price:
            # Degenerate range — place a single child at top_price
            return [{
                'idx': 0,
                'parent_level': ladder['level'],
                'buy_price': top_price,
                'sell_price': child_sell_for(top_price),
                'usdt_cost': total_usdt,
                'qty': total_usdt / top_price,
                'status': 'pending',
            }]

        # Choose child count based on target size, clamped to [min_n, max_n]
        raw_n = round(total_usdt / max(target_size, 1e-9))
        n = max(min_n, min(max_n, raw_n))
        # Need at least 2 to actually spread; fall back to 1 for tiny ladders
        if total_usdt < target_size * 1.5:
            n = 1

        # Reduce n so the smallest Fibonacci-weighted child still clears the
        # exchange's MIN_NOTIONAL filter. With weights [1, 1, 2, 3, 5, 8, 13]
        # at n=7 the top child is only 1/33 of total_usdt — for a $167 ladder
        # that's ~$5.06, which after qty step-rounding falls below Binance's
        # ~$5 min_notional and gets rejected. We trade fewer-but-valid children
        # for guaranteed placements rather than smaller-and-rejected ones.
        if notional_floor > 0 and n > 1:
            while n > 1:
                fib_n = _generate_fibonacci(n)
                smallest_child_usdt = total_usdt * (fib_n[0] / sum(fib_n))
                if smallest_child_usdt >= notional_floor:
                    break
                n -= 1
            # If even n=1 is below floor, the parent ladder itself is too
            # small for this symbol — keep n=1 and let the placement layer
            # surface the filter error with full context.

        if n == 1:
            return [{
                'idx': 0,
                'parent_level': ladder['level'],
                'buy_price': top_price,
                'sell_price': child_sell_for(top_price),
                'usdt_cost': total_usdt,
                'qty': total_usdt / top_price,
                'status': 'pending',
            }]

        # Price spacing: pick the strategy. All three end at bottom_price after n
        # steps and are sorted top-first.
        if spread_mode == 'linear':
            step = (top_price - bottom_price) / n
            child_prices = [top_price - step * (i + 1) for i in range(n)]
        elif spread_mode == 'fibonacci':
            # Legacy: log-Fibonacci weighting (clusters children near the top).
            fib_p = _generate_fibonacci(n)
            fib_total_p = float(sum(fib_p))
            full_drop_ratio = 1.0 - (bottom_price / top_price)
            target_log = -math.log(1.0 - full_drop_ratio)
            log_gaps = [target_log * (w / fib_total_p) for w in fib_p]
            child_prices = []
            cumulative_mult = 1.0
            for i in range(n):
                cumulative_mult *= math.exp(-log_gaps[i])
                child_prices.append(top_price * cumulative_mult)
        else:
            # 'geometric' (default): constant ratio between adjacent children
            # — evenly spaced in log space.
            ratio = (bottom_price / top_price) ** (1.0 / n)
            child_prices = [top_price * (ratio ** (i + 1)) for i in range(n)]

        # Sizing: keep Fibonacci-weighted USDT (deeper children carry more,
        # totaling exactly the parent's usdt_cost). This is independent of
        # the price spacing choice.
        fib = _generate_fibonacci(n)
        fib_total = float(sum(fib))

        children = []
        for i in range(n):
            child_price = child_prices[i]
            child_usdt = total_usdt * (fib[i] / fib_total)
            child_qty = child_usdt / child_price if child_price > 0 else 0
            children.append({
                'idx': i,
                'parent_level': ladder['level'],
                'buy_price': child_price,
                'sell_price': child_sell_for(child_price),
                'usdt_cost': child_usdt,
                'qty': child_qty,
                'status': 'pending',
            })

        return children

    def calculate_all_child_orders(self, min_notional=0.0):
        """Return {ladder_level: [children]} for every ladder (distribution mode).

        See calculate_child_orders for min_notional semantics.
        """
        result = {}
        for i, ladder in enumerate(self.ladders):
            next_buy = self.ladders[i + 1]['buy_price'] if i + 1 < len(self.ladders) else None
            result[ladder['level']] = self.calculate_child_orders(
                ladder, next_buy, min_notional=min_notional
            )
        return result

    def to_dict(self):
        """Export strategy as dictionary"""
        return {
            'name': self.config['name'],
            'pair': self.config['pair'],
            'ladders': self.ladders,
            'total_swing': self.ladders[-1]['cumulative_gap_percent'],
            'required_capital': self.calculate_required_capital()
        }
