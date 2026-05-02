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


# Valid sizing modes for the fib tier of BUY/SELL ladders. Micro tier is
# always flat 1x — that's the whole point of "always trading" small lots.
SIZE_MODES = ('martingale', 'fibonacci', 'fib_martingale')


def _resolve_fib_units(size_mode, fib_value, idx):
    """Compute the unit multiplier for a fib-tier ladder at index ``idx``.

    Modes:
      - "martingale" (legacy default): pure 2^i doubling, ignores fibonacci
        config. Aggressive — deepest ladders need huge capital.
      - "fibonacci": uses the configured fibonacci value directly. Each level
        scales as the fib sequence (1, 1, 2, 3, 5, 8, 13, 21 …) — gentler
        than 2^i and reachable with realistic capital.
      - "fib_martingale": fib * 2^i. Combines both — extreme growth, only
        useful with very few ladders.
    """
    if size_mode == 'fibonacci':
        return max(1, int(fib_value))
    if size_mode == 'fib_martingale':
        return max(1, int(fib_value)) * (2 ** idx)
    # default: 'martingale'
    return 2 ** idx


class Strategy:
    def __init__(self, config_path):
        self.config = self._load_config(config_path)
        self.ladders = []         # BUY ladders (below market)
        self.sell_ladders = []    # SELL accumulation ladders (above market)
        self._calculate_ladders()
        self._calculate_sell_ladders()

    def _load_config(self, config_path):
        """Load strategy configuration from JSON"""
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger.info(f"Loaded strategy: {config['name']}")
        return config

    @staticmethod
    def _resolve_size_mode(value):
        """Validate ``size_mode`` config; default to legacy 'martingale'.

        We keep the default as 'martingale' (current 2^i behaviour) so
        existing strategy JSON files behave identically. Strategies that
        want true Fibonacci sizing must opt in via ``"size_mode":
        "fibonacci"`` (or ``"fib_martingale"`` for fib × 2^i).
        """
        if value is None:
            return 'martingale'
        v = str(value).strip().lower()
        if v not in SIZE_MODES:
            logger.warning(
                f"Unknown size_mode {value!r}; falling back to 'martingale'. "
                f"Valid options: {SIZE_MODES}"
            )
            return 'martingale'
        return v

    def _calculate_ladders(self):
        """Calculate BUY ladder levels using compound multiplicative gaps.

        Two tiers of ladders, both below market:
          - micro tier (optional, prepended): tight gaps + flat sizing for the
            "always trading" feel — small lots that fill on minor dips.
          - fibonacci tier: classic 2^i martingale with widening fib gaps for
            big swings.

        Each level's gap is applied to the remaining price:
            buy_multiplier = (1 - gap₁) × (1 - gap₂) × (1 - gap₃) × ...

        This guarantees buy_multiplier stays positive (never reaches zero).
        The sell price for a level is the buy price of the level above it
        (or starting price for the topmost ladder).
        """
        ladder_config = self.config['ladder_config']

        self.ladders = []
        buy_multiplier = 1.0
        idx = 0

        # ---- Micro tier (optional, prepended) ------------------------------
        micro_cfg = ladder_config.get('micro_layer') or {}
        if micro_cfg.get('enabled'):
            micro_count = int(micro_cfg.get('count', 3))
            micro_gap = float(micro_cfg.get('gap', 0.004))
            micro_fib = micro_cfg.get('fibonacci') or [1] * micro_count
            for i in range(micro_count):
                fib = micro_fib[i] if i < len(micro_fib) else 1
                gap = micro_gap * fib
                prev_multiplier = buy_multiplier
                buy_multiplier *= (1 - gap)
                sell_multiplier = prev_multiplier if idx > 0 else 1.0
                self.ladders.append({
                    'level': -(idx + 1),
                    'tier': 'micro',
                    'fibonacci': fib,
                    'raw_gap_percent': gap,
                    'gap_percent': gap,
                    'cumulative_gap_percent': 1 - buy_multiplier,
                    'buy_price_multiplier': buy_multiplier,
                    'sell_price_multiplier': sell_multiplier,
                    'units': 1,  # micro ladders stay flat-sized — keeps the
                                 # "always trading" feel even when the fib tier
                                 # is configured to scale aggressively.
                    'size_mode': 'flat',
                    'status': 'pending',
                })
                idx += 1

        # ---- Fibonacci tier -------------------------------------------------
        base_gap = ladder_config['base_gap']
        fibonacci = ladder_config['fibonacci']
        num_ladders = ladder_config['ladders']
        gap_max = ladder_config.get('gap_max', 0.95)
        size_mode = self._resolve_size_mode(ladder_config.get('size_mode'))
        self._buy_size_mode = size_mode

        for i in range(num_ladders):
            fib = fibonacci[i]
            raw_gap = base_gap * fib
            gap = min(raw_gap, gap_max)
            prev_multiplier = buy_multiplier
            buy_multiplier *= (1 - gap)
            sell_multiplier = prev_multiplier if idx > 0 else 1.0
            self.ladders.append({
                'level': -(idx + 1),
                'tier': 'fib',
                'fibonacci': fib,
                'raw_gap_percent': raw_gap,
                'gap_percent': gap,
                'cumulative_gap_percent': 1 - buy_multiplier,
                'buy_price_multiplier': buy_multiplier,
                'sell_price_multiplier': sell_multiplier,
                'units': _resolve_fib_units(size_mode, fib, i),
                'size_mode': size_mode,
                'status': 'pending',
            })
            idx += 1

        if self.ladders:
            logger.info(f"Calculated {len(self.ladders)} BUY ladders "
                        f"[fib size_mode={getattr(self, '_buy_size_mode', 'martingale')}] "
                        f"with total swing: "
                        f"{self.ladders[-1]['cumulative_gap_percent']:.2%}")
        else:
            logger.info("No BUY ladders calculated")

    def _calculate_sell_ladders(self):
        """Calculate SELL accumulation ladders above market price.

        Mirror image of BUY ladders for coin accumulation:
          - SELL above current price → fill on rallies, deliver USDT
          - Auto-buyback at sell_price / (1 + coin_profit_percent) → buys
            BACK MORE coins than sold, locking in coin gains.

        Sell multiplier compounds upward:
            sell_multiplier = (1 + gap₁) × (1 + gap₂) × ...

        Buyback multiplier for each level is the previous level's sell
        multiplier (or 1.0 for the topmost). On fill, we ALSO ensure the
        actual buyback price never exceeds sell_price / (1 + coin_profit)
        so a coin gain is guaranteed regardless of how the structure stacks.

        No-op when accumulation is disabled.
        """
        accum = self.config.get('accumulation') or {}
        self.sell_ladders = []
        if not accum.get('enabled'):
            return

        sell_multiplier = 1.0
        idx = 0

        # ---- Micro tier (optional, prepended) ------------------------------
        micro_cfg = accum.get('micro_layer') or {}
        if micro_cfg.get('enabled'):
            micro_count = int(micro_cfg.get('count', 3))
            micro_gap = float(micro_cfg.get('gap', 0.004))
            micro_fib = micro_cfg.get('fibonacci') or [1] * micro_count
            for i in range(micro_count):
                fib = micro_fib[i] if i < len(micro_fib) else 1
                gap = micro_gap * fib
                prev_multiplier = sell_multiplier
                sell_multiplier *= (1 + gap)
                buyback_multiplier = prev_multiplier if idx > 0 else 1.0
                self.sell_ladders.append({
                    'level': idx + 1,
                    'tier': 'micro',
                    'fibonacci': fib,
                    'gap_percent': gap,
                    'cumulative_gap_percent': sell_multiplier - 1,
                    'sell_price_multiplier': sell_multiplier,
                    'buyback_price_multiplier': buyback_multiplier,
                    'units': 1,  # micro stays flat (matches BUY-side micro)
                    'size_mode': 'flat',
                    'status': 'pending',
                })
                idx += 1

        # ---- Fibonacci tier -------------------------------------------------
        base_gap = float(accum.get('base_gap', 0.04))
        fibonacci = accum.get('fibonacci') or [1, 1, 2, 3, 5, 8, 13, 21]
        num_ladders = int(accum.get('ladders', 8))
        gap_max = float(accum.get('gap_max', 0.60))
        size_mode = self._resolve_size_mode(accum.get('size_mode'))
        self._sell_size_mode = size_mode

        for i in range(num_ladders):
            fib = fibonacci[i] if i < len(fibonacci) else fibonacci[-1]
            raw_gap = base_gap * fib
            gap = min(raw_gap, gap_max)
            prev_multiplier = sell_multiplier
            sell_multiplier *= (1 + gap)
            buyback_multiplier = prev_multiplier if idx > 0 else 1.0
            self.sell_ladders.append({
                'level': idx + 1,
                'tier': 'fib',
                'fibonacci': fib,
                'raw_gap_percent': raw_gap,
                'gap_percent': gap,
                'cumulative_gap_percent': sell_multiplier - 1,
                'sell_price_multiplier': sell_multiplier,
                'buyback_price_multiplier': buyback_multiplier,
                'units': _resolve_fib_units(size_mode, fib, i),
                'size_mode': size_mode,
                'status': 'pending',
            })
            idx += 1

        if self.sell_ladders:
            logger.info(f"Calculated {len(self.sell_ladders)} SELL (accumulation) ladders "
                        f"[fib size_mode={getattr(self, '_sell_size_mode', 'martingale')}] "
                        f"with total swing: +{self.sell_ladders[-1]['cumulative_gap_percent']:.2%}")

    def update_prices(self, current_price):
        """Update both BUY and SELL ladder prices from current market price."""
        self._update_buy_ladder_prices(current_price)
        self._update_sell_ladder_prices(current_price)

    def _update_buy_ladder_prices(self, current_price):
        """Update BUY ladder prices and quantities.

        Sizing is tier-aware:
          - micro tier: flat unit_size_zec_micro coins per ladder (taken from
            ladder_config.micro_layer.unit_size_zec, falling back to the main
            unit_size_zec scaled down). Keeps inner ladders affordable so
            small dips trigger small buys.
          - fib tier: martingale, units × base_usdt_per_unit, where
            base_usdt_per_unit = top_fib_buy_price × unit_size_zec.
        """
        if not self.ladders:
            return

        ladder_config = self.config['ladder_config']
        unit_size_key = f"unit_size_{self.config['pair'][:3].lower()}"
        unit_size = ladder_config.get(unit_size_key, 0.01)

        micro_cfg = ladder_config.get('micro_layer') or {}
        micro_unit = micro_cfg.get(unit_size_key, unit_size * 0.1) if micro_cfg.get('enabled') else 0

        # Find the topmost fib ladder so its buy price anchors the
        # martingale base (matches legacy behavior when no micro tier exists).
        first_fib = next((l for l in self.ladders if l.get('tier', 'fib') == 'fib'), self.ladders[0])
        first_fib_buy_price = current_price * first_fib['buy_price_multiplier']
        base_usdt_per_unit_fib = first_fib_buy_price * unit_size

        for ladder in self.ladders:
            ladder['buy_price'] = current_price * ladder['buy_price_multiplier']
            ladder['sell_price'] = current_price * ladder['sell_price_multiplier']

            if ladder['buy_price'] <= 0 or ladder['sell_price'] <= 0:
                logger.error(f"Invalid price at level {ladder['level']}: "
                           f"buy=${ladder['buy_price']:.2f}, sell=${ladder['sell_price']:.2f}. Skipping.")
                continue

            tier = ladder.get('tier', 'fib')
            if tier == 'micro':
                ladder['btc_amount'] = micro_unit * ladder['units']
                ladder['usdt_cost'] = ladder['btc_amount'] * ladder['buy_price']
            else:
                ladder['usdt_cost'] = ladder['units'] * base_usdt_per_unit_fib
                ladder['btc_amount'] = ladder['usdt_cost'] / ladder['buy_price']

    def _update_sell_ladder_prices(self, current_price):
        """Update SELL accumulation ladder prices and quantities.

        Each ladder sells `units × unit_size_zec` coins. Buyback price is
        capped at sell_price / (1 + coin_profit_percent) so the buyback
        ALWAYS yields more coins than were sold, regardless of how the
        ladder structure stacks.
        """
        if not self.sell_ladders:
            return

        accum = self.config.get('accumulation') or {}
        coin_profit = max(0.0, float(accum.get('coin_profit_percent', 0.005)))
        unit_size_key = f"unit_size_{self.config['pair'][:3].lower()}"
        unit_size = float(accum.get(unit_size_key, 0.05))

        micro_cfg = accum.get('micro_layer') or {}
        micro_unit = float(micro_cfg.get(unit_size_key, unit_size * 0.6)) if micro_cfg.get('enabled') else 0

        for ladder in self.sell_ladders:
            ladder['sell_price'] = current_price * ladder['sell_price_multiplier']
            structural_buyback = current_price * ladder['buyback_price_multiplier']

            if ladder['sell_price'] <= 0:
                logger.error(f"Invalid SELL price at accum level {ladder['level']}: "
                           f"${ladder['sell_price']:.4f}. Skipping.")
                continue

            tier = ladder.get('tier', 'fib')
            base_unit = micro_unit if tier == 'micro' else unit_size
            ladder['coin_amount'] = base_unit * ladder['units']

            # Cap buyback so we ALWAYS gain coins (sell_price / (1+profit)
            # gives qty back × (1+profit)). Take the lower of structural and
            # capped → guarantees coin gain even if structure is degenerate.
            profit_capped_buyback = ladder['sell_price'] / (1.0 + coin_profit)
            ladder['buyback_price'] = min(structural_buyback, profit_capped_buyback)
            ladder['expected_buyback_qty'] = ladder['coin_amount'] * (
                ladder['sell_price'] / ladder['buyback_price']
            )
            ladder['expected_coin_gain'] = ladder['expected_buyback_qty'] - ladder['coin_amount']
            ladder['usdt_received'] = ladder['coin_amount'] * ladder['sell_price']

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

    def is_accumulation_enabled(self):
        """Check whether SELL-side coin accumulation is configured."""
        return bool((self.config.get('accumulation') or {}).get('enabled'))

    def get_accumulation_config(self):
        """Return accumulation-mode config with defaults applied.

        SELL-side accumulation runs in parallel with the BUY ladder. When the
        market rallies and a SELL_ACCUM fills, an immediate BUY_BACK is
        placed at sell_price / (1 + coin_profit_percent), so each round-trip
        delivers MORE coins than were sold (capped to guarantee a coin gain).

        Keys:
          - enabled: master switch (default False — opt-in)
          - coin_profit_percent: minimum coin gain per round-trip (e.g. 0.005
            = 0.5% more coins back per cycle)
          - proximity_percent: place SELL when current price is within X% of
            its sell_price (mirrors BUY-side proximity)
          - max_open_sells_cap: hard cap on simultaneous SELL_ACCUM orders
          - reserve_coin_percent: keep this fraction of the coin allocation
            unbooked (safety buffer for fees and rounding)
        """
        accum = self.config.get('accumulation') or {}
        return {
            'enabled': bool(accum.get('enabled', False)),
            'coin_profit_percent': float(accum.get('coin_profit_percent', 0.005)),
            'proximity_percent': float(accum.get('proximity_percent', 0.006)),
            'max_open_sells_cap': int(accum.get('max_open_sells_cap', 30)),
            'reserve_coin_percent': float(accum.get('reserve_coin_percent', 0.05)),
            # When coin balance is insufficient, wait this long for a dip /
            # upstream BUYs to refill before downsizing the SELL.
            'wait_for_balance_seconds': float(accum.get('wait_for_balance_seconds', 600)),
            # If True, after the wait window elapses, place a SELL sized to
            # the actually-available balance (subject to LOT_SIZE / NOTIONAL).
            'allow_partial_after_wait': bool(accum.get('allow_partial_after_wait', True)),
        }

    def get_pending_sell_ladders(self):
        """Get accumulation SELL ladders waiting to be triggered."""
        return [l for l in self.sell_ladders if l['status'] == 'pending']

    def get_active_sell_ladders(self):
        """Get accumulation SELL ladders currently placed on the exchange."""
        return [l for l in self.sell_ladders if l['status'] in ('placed', 'awaiting_buyback')]

    def reset_sell_ladders(self):
        """Reset closed accumulation ladders to pending for next cycle."""
        reset_count = 0
        for ladder in self.sell_ladders:
            if ladder['status'] == 'closed':
                ladder['status'] = 'pending'
                reset_count += 1
        if reset_count:
            logger.info(f"Reset {reset_count} SELL ladders to pending for new cycle")

    def all_sell_ladders_closed(self):
        """All sell ladders have completed accumulation cycle."""
        if not self.sell_ladders:
            return True
        return all(l['status'] == 'closed' for l in self.sell_ladders if l['status'] != 'pending') and \
               any(l['status'] == 'closed' for l in self.sell_ladders)

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
            'sell_ladders': self.sell_ladders,
            'total_swing': self.ladders[-1]['cumulative_gap_percent'] if self.ladders else 0,
            'required_capital': self.calculate_required_capital(),
        }
