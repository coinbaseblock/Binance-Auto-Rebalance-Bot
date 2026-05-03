"""
Order Manager - Handle order placement and tracking
"""
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, binance_client, portfolio):
        self.client = binance_client
        self.portfolio = portfolio
        self.active_orders = {}  # {order_id: order_details}
        self._sequential_state = {}  # {strategy_name: {next_level_idx, last_order_time}}
        self._insufficient_balance_warned = {}  # {strategy_name: {level, last_warn_time}} - periodic warnings
        # Distribution mode: pending children that have not been sent to Binance yet.
        # {strategy_name: list[child_dict]} — children are promoted once price is close
        # enough and there is an open-order slot available.
        self._pending_children = {}
        # Recovery mode: per-strategy pool of underwater children waiting to be
        # sold together as one merged SELL once price bounces above their
        # combined cost basis. See Strategy.get_recovery_config() for details.
        # {strategy_name: {
        #     'children': [child_dict, ...],     # serialized snapshots; parent_ladder linked
        #     'merged_sell_order_id': int|None,  # currently active merged SELL on Binance
        #     'merged_sell_price': float|None,   # last placed price
        #     'merged_sell_qty': float|None,     # last placed total quantity
        # }}
        self._recovery_lots = {}
        # Throttle for stale-SELL scans so we don't hammer the exchange.
        # {strategy_name: last_check_unix_ts}
        self._last_stale_check = {}
        # Throttle for stale-BUY auto-cancel scans (different cadence/policy
        # than the SELL stale check). {strategy_name: last_check_unix_ts}
        self._last_stale_buy_check = {}
        # Strategy lookup so close handlers in check_filled_orders can read
        # config and trigger per-ladder recycling without main.py wiring.
        # Populated via register_strategies().
        self._strategies_by_name = {}
        # Accumulation (SELL-then-BUY-back) cycle stats.
        # {strategy_name: {
        #     'coin_gain_total': float,    # cumulative coins gained from cycles
        #     'cycles_completed': int,     # number of full SELL→BUY round-trips
        # }}
        self._accumulation_stats = {}
        # Inventory hoard (mini-scalper) state — isolated from main ladders.
        # {strategy_name: {
        #     'budget_used_usdt': float,    # USDT currently locked in open hoard BUYs
        #     'recent_buy_ts': [unix_ts],   # timestamps of recent hoard BUY placements
        #     'last_buy_ts': float,         # cooldown anchor
        # }}
        self._hoard_state = {}
        # {strategy_name: {coin_gain, usdt_gain, cycles}}
        self._hoard_stats = {}

    def place_ladder_buy_orders(self, strategy, current_price):
        """Place buy orders for all pending ladders, respecting balance and exchange filters"""
        orders_placed = []
        skipped_balance = 0
        skipped_price_filter = 0

        # Get current available USDT balance from exchange
        try:
            balance = self.client.get_account_balance('USDT')
            available_usdt = balance['free']
        except Exception as e:
            logger.error(f"Cannot fetch USDT balance, aborting order placement: {e}")
            return orders_placed

        symbol = strategy.config['pair']

        for ladder in strategy.get_pending_ladders():
            if ladder['buy_price'] >= current_price:
                continue  # Only place orders below current price

            # Estimate order cost (price × quantity)
            estimated_cost = ladder['buy_price'] * ladder['btc_amount']

            # Check if we have enough balance
            if estimated_cost > available_usdt:
                skipped_balance += 1
                logger.warning(f"Skipping level {ladder['level']}: need ${estimated_cost:.2f} "
                             f"but only ${available_usdt:.2f} USDT available")
                continue

            # Check PERCENT_PRICE_BY_SIDE filter
            ok, reason = self.client.check_percent_price_filter(symbol, 'BUY', ladder['buy_price'])
            if not ok:
                skipped_price_filter += 1
                logger.warning(f"Skipping level {ladder['level']}: {reason}")
                continue

            try:
                order = self.client.create_limit_order(
                    symbol=symbol,
                    side='BUY',
                    quantity=ladder['btc_amount'],
                    price=ladder['buy_price']
                )

                self.active_orders[order['orderId']] = {
                    'strategy': strategy.config['name'],
                    'level': ladder['level'],
                    'type': 'BUY',
                    'order': order,
                    'ladder': ladder,
                    'placed_at': time.time(),
                }

                orders_placed.append(order)
                available_usdt -= estimated_cost  # Track remaining balance
                logger.info(f"Buy order placed: Level {ladder['level']} @ ${ladder['buy_price']:.2f}")

            except Exception as e:
                logger.error(f"Failed to place buy order for level {ladder['level']}: {e}")

        # Summary
        total = len(strategy.get_pending_ladders())
        logger.info(f"Order placement summary: {len(orders_placed)}/{total} placed"
                    + (f", {skipped_balance} skipped (insufficient balance)" if skipped_balance else "")
                    + (f", {skipped_price_filter} skipped (price filter)" if skipped_price_filter else ""))

        return orders_placed

    def place_next_sequential_order(self, strategy, current_price):
        """Place only the next pending buy order when price approaches.

        Sequential mode: instead of placing all ladder orders at once,
        place them one at a time. Wait until the price is within
        proximity_percent of the next level, then wait delay_seconds
        before placing the order.

        Returns the placed order or None.
        """
        placement_config = strategy.config.get('order_placement', {})
        proximity_pct = placement_config.get('proximity_percent', 0.015)
        delay_secs = placement_config.get('delay_seconds', 45)

        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']

        # Initialize sequential state for this strategy
        if strategy_name not in self._sequential_state:
            self._sequential_state[strategy_name] = {
                'next_level_idx': 0,
                'approaching_since': None,
            }

        state = self._sequential_state[strategy_name]

        # Check if we already have an active buy order for this strategy
        has_active_buy = any(
            od['strategy'] == strategy_name and od['type'] == 'BUY'
            for od in self.active_orders.values()
        )
        if has_active_buy:
            return None  # Wait for the current order to fill or be cancelled

        # Find the next pending ladder
        pending = strategy.get_pending_ladders()
        if not pending:
            return None

        next_ladder = pending[0]

        # For the very first order, place immediately (it's the closest to price)
        if state['next_level_idx'] == 0 and state['approaching_since'] is None:
            return self._place_single_buy_order(strategy, next_ladder, current_price, symbol)

        # Check if price has already dropped below the buy price
        # In this case, place the order immediately (limit buy above market fills at market)
        if current_price <= next_ladder['buy_price']:
            logger.info(f"[{strategy_name}] Price ${current_price:.2f} already below "
                       f"level {next_ladder['level']} buy @ ${next_ladder['buy_price']:.2f}, "
                       f"placing order immediately")
            state['approaching_since'] = None
            return self._place_single_buy_order(strategy, next_ladder, current_price, symbol)

        # Check if price is approaching the next ladder's buy price
        distance_pct = (current_price - next_ladder['buy_price']) / current_price
        if distance_pct <= proximity_pct:
            now = time.time()

            if state['approaching_since'] is None:
                # Price just entered proximity zone - start the delay timer
                state['approaching_since'] = now
                logger.info(f"[{strategy_name}] Price ${current_price:.2f} approaching "
                           f"level {next_ladder['level']} @ ${next_ladder['buy_price']:.2f} "
                           f"(distance: {distance_pct:.2%}), waiting {delay_secs}s...")
                return None

            elapsed = now - state['approaching_since']
            if elapsed >= delay_secs:
                # Delay elapsed, place the order
                state['approaching_since'] = None
                return self._place_single_buy_order(strategy, next_ladder, current_price, symbol)
            else:
                remaining = delay_secs - elapsed
                logger.debug(f"[{strategy_name}] Waiting {remaining:.0f}s more before placing "
                            f"level {next_ladder['level']}...")
                return None
        else:
            # Price moved away - reset the timer
            if state['approaching_since'] is not None:
                state['approaching_since'] = None
                logger.info(f"[{strategy_name}] Price moved away from level {next_ladder['level']}, "
                           f"resetting timer")
            return None

    def _place_single_buy_order(self, strategy, ladder, current_price, symbol):
        """Place a single buy order for one ladder level.

        Note: If buy_price >= current_price, this is still valid. On Binance,
        a limit buy order priced above the market will fill immediately at the
        current market price (like a market order with price protection).
        """
        strategy_name = strategy.config['name']

        # Check available balance
        try:
            balance = self.client.get_account_balance('USDT')
            available_usdt = balance['free']
        except Exception as e:
            logger.error(f"Cannot fetch USDT balance: {e}")
            return None

        estimated_cost = ladder['buy_price'] * ladder['btc_amount']
        if estimated_cost > available_usdt:
            # Warn periodically (every 5 minutes) so user knows to deposit funds
            warn_interval = 300  # 5 minutes
            now = time.time()
            warn_info = self._insufficient_balance_warned.get(strategy_name, {})
            should_warn = (
                warn_info.get('level') != ladder['level'] or
                now - warn_info.get('last_warn_time', 0) >= warn_interval
            )
            if should_warn:
                shortfall = estimated_cost - available_usdt
                logger.warning(f"[{strategy_name}] Insufficient balance for level {ladder['level']}: "
                             f"need ${estimated_cost:.2f} but only ${available_usdt:.2f} USDT available "
                             f"(short ${shortfall:.2f}). Deposit more USDT to continue.")
                self._insufficient_balance_warned[strategy_name] = {
                    'level': ladder['level'],
                    'last_warn_time': now,
                }
            return None

        # Check PERCENT_PRICE_BY_SIDE filter
        ok, reason = self.client.check_percent_price_filter(symbol, 'BUY', ladder['buy_price'])
        if not ok:
            logger.warning(f"[{strategy_name}] Skipping level {ladder['level']}: {reason}")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol,
                side='BUY',
                quantity=ladder['btc_amount'],
                price=ladder['buy_price']
            )

            self.active_orders[order['orderId']] = {
                'strategy': strategy_name,
                'level': ladder['level'],
                'type': 'BUY',
                'order': order,
                'ladder': ladder,
                'placed_at': time.time(),
            }

            # Advance sequential state
            if strategy_name in self._sequential_state:
                self._sequential_state[strategy_name]['next_level_idx'] += 1
                self._sequential_state[strategy_name]['approaching_since'] = None

            # Clear insufficient balance warning since we successfully placed
            self._insufficient_balance_warned.pop(strategy_name, None)

            logger.info(f"[{strategy_name}] Sequential buy order placed: "
                       f"Level {ladder['level']} @ ${ladder['buy_price']:.2f}")
            return order

        except Exception as e:
            logger.error(f"[{strategy_name}] Failed to place buy order for level {ladder['level']}: {e}")
            return None

    def reset_sequential_state(self, strategy_name):
        """Reset sequential placement state for a strategy (e.g., on new cycle)."""
        if strategy_name in self._sequential_state:
            del self._sequential_state[strategy_name]
        self._insufficient_balance_warned.pop(strategy_name, None)

    def is_sequential_mode(self, strategy):
        """Check if a strategy uses sequential order placement."""
        return strategy.config.get('order_placement', {}).get('mode') == 'sequential'

    def is_distribution_mode(self, strategy):
        """Check if a strategy uses distribution (spread-child) order placement."""
        return strategy.config.get('order_placement', {}).get('mode') == 'distribution'

    def reset_distribution_state(self, strategy_name):
        """Clear pending child queue (e.g. on cycle restart)."""
        self._pending_children.pop(strategy_name, None)
        self._insufficient_balance_warned.pop(strategy_name, None)
        # Recovery state belongs to the cycle too — once the cycle ends, any
        # surviving recovery lot has either filled (handled via the normal
        # SELL_MERGED path) or the user issued a full reset.
        self._recovery_lots.pop(strategy_name, None)
        self._last_stale_check.pop(strategy_name, None)

    def _get_symbol_min_notional(self, symbol):
        """Fetch the symbol's MIN_NOTIONAL filter value (cached by client).

        Returns 0.0 on any failure so callers fall back to no floor (the
        exchange will still reject; the placement-time safety check
        (_child_meets_notional) is the second line of defence).
        """
        try:
            filters = self.client.get_symbol_filters(symbol)
            return float(filters.get('min_notional', 0) or 0)
        except Exception as e:
            logger.warning(f"Could not fetch min_notional for {symbol}: {e}")
            return 0.0

    def log_planned_distribution(self, strategy):
        """Log planned children per ladder for visibility."""
        if not self.is_distribution_mode(strategy):
            return
        min_notional = self._get_symbol_min_notional(strategy.config['pair'])
        all_children = strategy.calculate_all_child_orders(min_notional=min_notional)
        strategy_name = strategy.config['name']
        total_children = sum(len(cs) for cs in all_children.values())
        logger.info(f"[{strategy_name}] Distribution plan: {total_children} child orders "
                    f"across {len(all_children)} ladder levels (placed as price approaches)")
        for level, children in all_children.items():
            if not children:
                continue
            top = children[0]
            bot = children[-1]
            logger.info(f"  Level {level:>3}: {len(children)} children | "
                       f"top ${top['buy_price']:.4f} → bot ${bot['buy_price']:.4f} | "
                       f"sells ${top['sell_price']:.4f} → ${bot['sell_price']:.4f} | "
                       f"total ${sum(c['usdt_cost'] for c in children):.2f}")

    def prime_distribution_queue(self, strategy):
        """Build the pending-children queue (no order placement). Idempotent —
        a no-op when the queue is already populated (e.g. restored from saved
        state). Used by reconcile so orphan orders can be matched to children.
        """
        if not self.is_distribution_mode(strategy):
            return
        strategy_name = strategy.config['name']
        if strategy_name in self._pending_children:
            return

        min_notional = self._get_symbol_min_notional(strategy.config['pair'])
        queue = []
        all_children = strategy.calculate_all_child_orders(min_notional=min_notional)
        for ladder in strategy.get_pending_ladders():
            ladder_children = all_children.get(ladder['level'], [])
            ladder['children_total'] = len(ladder_children)
            ladder.setdefault('children_closed', 0)
            ladder.setdefault('children_placed', 0)
            for child in ladder_children:
                child['parent_ladder'] = ladder
            queue.extend(ladder_children)
        queue.sort(key=lambda c: c['buy_price'], reverse=True)
        self._pending_children[strategy_name] = queue
        logger.info(f"[{strategy_name}] Distribution queue initialized with {len(queue)} children")

    def register_strategies(self, strategies):
        """Register all loaded strategies so close-time handlers can resolve
        a Strategy object from a strategy name (e.g. for per-ladder recycle).

        Idempotent — re-registering simply replaces the lookup table.
        """
        self._strategies_by_name = {
            s.config['name']: s for s in strategies if s and s.config.get('name')
        }

    def _strategy_by_name(self, name):
        """Return the registered Strategy for ``name`` (or None)."""
        return self._strategies_by_name.get(name)

    def _recycle_in_place_enabled(self, strategy_or_name):
        """Whether the strategy opted into per-ladder in-place recycling.

        Default False — preserves the legacy global-restart behaviour for
        existing strategies. Strategies opt in with
        ``order_placement.recycle_in_place: true``.
        """
        strategy = strategy_or_name
        if isinstance(strategy_or_name, str):
            strategy = self._strategy_by_name(strategy_or_name)
        if strategy is None:
            return False
        placement = (strategy.config.get('order_placement') or {})
        return bool(placement.get('recycle_in_place', False))

    def _recycle_ladder_in_place(self, strategy_name, ladder):
        """Re-arm a fully-closed BUY ladder *in place* — same buy/sell prices,
        same child layout. Lets the bot keep cycling a level repeatedly without
        triggering the global auto-restart (which re-anchors every other
        ladder to the new market price, discarding the original fib targets
        that were waiting on a deeper dip).
        """
        if ladder is None or ladder.get('status') != 'closed':
            return
        strategy = self._strategy_by_name(strategy_name)
        if strategy is None:
            logger.debug(f"[{strategy_name}] Recycle skipped — strategy not registered")
            return

        # Reset ladder counters
        ladder['status'] = 'pending'
        ladder['children_closed'] = 0
        ladder['children_placed'] = 0

        # Rebuild children from the ladder's existing buy/sell prices (NOT
        # current market) so re-armed orders sit at the same targets.
        min_notional = self._get_symbol_min_notional(strategy.config['pair'])
        next_buy = None
        for i, l in enumerate(strategy.ladders):
            if l is ladder:
                if i + 1 < len(strategy.ladders):
                    next_buy = strategy.ladders[i + 1].get('buy_price')
                break
        children = strategy.calculate_child_orders(
            ladder, next_buy, min_notional=min_notional
        )
        ladder['children_total'] = len(children)
        for child in children:
            child['parent_ladder'] = ladder

        # Drop any stale pending children for this ladder before re-adding
        # the freshly-rebuilt set. In normal flow the queue holds nothing for
        # a closed ladder (everything got placed), but defending against
        # half-promoted state keeps recycle idempotent.
        queue = self._pending_children.setdefault(strategy_name, [])
        queue[:] = [c for c in queue if c.get('parent_level') != ladder.get('level')]
        queue.extend(children)
        queue.sort(key=lambda c: c['buy_price'], reverse=True)

        logger.info(
            f"[{strategy_name}] Ladder L{ladder['level']} recycled in place "
            f"(BUY @ ${ladder.get('buy_price', 0):.4f} → "
            f"SELL @ ${ladder.get('sell_price', 0):.4f}, "
            f"{len(children)} children re-armed)"
        )

    def _recycle_sell_ladder_in_place(self, strategy_name, sell_ladder):
        """Re-arm a closed SELL accumulation ladder. SELL ladders don't have
        children — they're a single SELL order pre-placed when price
        approaches their target — so recycling is just resetting the status
        flag back to pending.
        """
        if sell_ladder is None or sell_ladder.get('status') != 'closed':
            return
        sell_ladder['status'] = 'pending'
        sell_ladder.pop('filled_qty', None)
        sell_ladder.pop('filled_price', None)
        sell_ladder.pop('pending_buyback', None)
        logger.info(
            f"[{strategy_name}] SELL ladder Lvl +{sell_ladder.get('level')} "
            f"recycled in place (SELL @ ${sell_ladder.get('sell_price', 0):.4f})"
        )

    def place_distribution_orders(self, strategy, current_price):
        """Entry point for distribution mode: build pending queue (if empty),
        recycle stale BUY orders to free up cap slots, then promote children
        whose price is near the market.

        Returns list of orders placed this call (may be empty when nothing is
        yet in proximity).
        """
        self.prime_distribution_queue(strategy)
        # Free up slots from far-and-old BUYs before trying to promote new
        # ones — otherwise the cap can wedge us out of placing closer orders.
        self._scan_stale_buys(strategy, current_price)
        return self._promote_pending_children(strategy, current_price)

    @staticmethod
    def _extract_order_time(order_obj):
        """Return a unix timestamp for when ``order_obj`` was created.

        Binance order payloads carry ``time`` (ms since epoch); fall back to
        ``transactTime`` and finally to "now" so newly-adopted orphans without
        a recorded time aren't immediately treated as ancient.
        """
        for key in ('time', 'transactTime'):
            raw = (order_obj or {}).get(key)
            if raw:
                try:
                    return float(raw) / 1000.0
                except (TypeError, ValueError):
                    pass
        return time.time()

    def _scan_stale_buys(self, strategy, current_price):
        """Cancel BUY orders that are both old AND far below the market.

        When the bot has been running through a sideways or rising market,
        deep BUYs from earlier dips can pile up against the per-symbol open-
        order cap, blocking the placement of *closer* (more useful) BUYs.
        This scan reclaims those slots: a BUY is recycled when

          - it has been sitting open for more than ``stale_buy_max_age_seconds``
            AND
          - it sits more than ``stale_buy_distance_threshold`` below the
            current price (so it is unlikely to fill any time soon).

        Cancelled BUYs are dropped from ``active_orders`` and the underlying
        child returns to the pending queue, so the regular proximity check
        will re-place it when the market actually approaches its price.

        Disabled when ``stale_buy_max_age_seconds`` is 0 / unset. Throttled
        by ``stale_buy_check_interval_seconds`` so we don't hammer the
        exchange every loop tick.
        """
        if current_price is None or current_price <= 0:
            return 0
        placement = strategy.config.get('order_placement', {}) or {}
        max_age = float(placement.get('stale_buy_max_age_seconds', 0) or 0)
        if max_age <= 0:
            return 0
        distance_threshold = float(
            placement.get('stale_buy_distance_threshold', 0.05) or 0.05
        )
        check_interval = float(
            placement.get('stale_buy_check_interval_seconds', 600) or 600
        )

        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        now = time.time()
        last = self._last_stale_buy_check.get(strategy_name, 0)
        if now - last < check_interval:
            return 0
        self._last_stale_buy_check[strategy_name] = now

        cancelled = 0
        queue = self._pending_children.setdefault(strategy_name, [])

        for oid, od in list(self.active_orders.items()):
            if od.get('strategy') != strategy_name:
                continue
            if od.get('type') != 'BUY':
                continue
            order_obj = od.get('order') or {}
            try:
                buy_price = float(order_obj.get('price') or 0)
            except (TypeError, ValueError):
                continue
            if buy_price <= 0 or buy_price >= current_price:
                continue
            distance = (current_price - buy_price) / current_price
            if distance < distance_threshold:
                continue
            placed_at = od.get('placed_at') or 0
            age = now - placed_at if placed_at else 0
            if age < max_age:
                continue

            try:
                self.client.cancel_order(symbol=symbol, order_id=oid)
            except Exception as e:
                logger.warning(
                    f"[{strategy_name}] Stale BUY cancel failed for {oid}: {e}"
                )
                continue

            self.active_orders.pop(oid, None)
            child = od.get('child')
            parent = od.get('ladder')
            if child is not None:
                child['status'] = 'pending'
                if parent is not None:
                    parent['children_placed'] = max(
                        0, int(parent.get('children_placed', 1)) - 1
                    )
                # Return to pending queue so the proximity check can re-place
                # it once the market actually approaches.
                if child not in queue:
                    queue.append(child)
            cancelled += 1
            logger.info(
                f"[{strategy_name}] Stale BUY recycled: orderId={oid} "
                f"L{od.get('level')} @ ${buy_price:.4f} "
                f"(age {age / 60:.1f}min, {distance:.2%} below market) "
                f"— slot freed for closer orders"
            )

        if cancelled:
            # Re-sort the queue top-first so promotion logic still picks the
            # closest-to-market child next.
            queue.sort(key=lambda c: c['buy_price'], reverse=True)
            logger.info(
                f"[{strategy_name}] Stale-BUY scan recycled {cancelled} order(s)"
            )
        return cancelled

    def _promote_pending_children(self, strategy, current_price):
        """Promote pending children to Binance when price is in proximity and
        the per-symbol open-order cap has room."""
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        dist_cfg = strategy.get_distribution_config()
        proximity = dist_cfg['proximity_percent']
        cap = dist_cfg['max_open_orders_cap']

        queue = self._pending_children.get(strategy_name, [])
        if not queue:
            return []

        placed = []
        # Count current open orders for this symbol (both BUY and SELL)
        symbol_open_orders = sum(
            1 for od in self.active_orders.values()
            if od['order'].get('symbol') == symbol
        )

        for child in list(queue):
            if symbol_open_orders >= cap:
                logger.debug(f"[{strategy_name}] Open-order cap ({cap}) reached for {symbol}, "
                             f"{len(queue)} children still pending")
                break

            # Promote when price is already at/below child price (limit buy will
            # fill immediately) OR within proximity above it.
            if current_price <= child['buy_price']:
                should_promote = True
            else:
                distance = (current_price - child['buy_price']) / current_price
                should_promote = distance <= proximity

            if not should_promote:
                # Children are sorted top-first; if this one isn't in range,
                # lower ones definitely aren't either.
                break

            # Placement-time NOTIONAL guard: if the rounded order is below the
            # symbol's min_notional, drop the child instead of letting Binance
            # reject it on every loop iteration forever. The strategy-side
            # n-cap should prevent this, but we keep the guard for resumed
            # state files predating the fix and for edge-cases like extreme
            # price moves between planning and placement.
            if not self._child_meets_notional(strategy, child):
                queue.remove(child)
                continue

            order = self._place_child_buy(strategy, child)
            if order is not None:
                queue.remove(child)
                placed.append(order)
                symbol_open_orders += 1

        return placed

    def _child_meets_notional(self, strategy, child):
        """Return True if the child's rounded notional clears MIN_NOTIONAL.

        Logs a clear warning and returns False otherwise so the caller can
        drop the child from the pending queue. Falls back to True (let the
        exchange decide) when filter info isn't available.
        """
        symbol = strategy.config['pair']
        try:
            filters = self.client.get_symbol_filters(symbol)
        except Exception:
            return True
        min_notional = float(filters.get('min_notional', 0) or 0)
        if min_notional <= 0:
            return True
        try:
            rp = float(self.client.round_price(symbol, child['buy_price']))
            rq = float(self.client.round_quantity(symbol, child['qty']))
        except Exception:
            return True
        rounded_notional = rp * rq
        if rounded_notional < min_notional:
            logger.warning(
                f"[{strategy.config['name']}] Dropping child "
                f"L{child['parent_level']}.{child['idx']}: rounded notional "
                f"${rounded_notional:.2f} below min_notional ${min_notional:.2f} "
                f"(planned ${child.get('usdt_cost', 0):.2f} @ ${child['buy_price']:.4f}). "
                f"Increase child_order_usdt or lower min_children_per_ladder so "
                f"per-child size clears the exchange filter."
            )
            return False
        return True

    def _place_child_buy(self, strategy, child):
        """Place a single child BUY order on Binance."""
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        parent_ladder = child['parent_ladder']

        try:
            balance = self.client.get_account_balance('USDT')
            available_usdt = balance['free']
        except Exception as e:
            logger.error(f"[{strategy_name}] Cannot fetch USDT balance: {e}")
            return None

        estimated_cost = child['buy_price'] * child['qty']
        if estimated_cost > available_usdt:
            warn_interval = 300
            now = time.time()
            warn_info = self._insufficient_balance_warned.get(strategy_name, {})
            key = (child['parent_level'], child['idx'])
            if warn_info.get('key') != key or now - warn_info.get('last_warn_time', 0) >= warn_interval:
                logger.warning(f"[{strategy_name}] Insufficient balance for child "
                             f"L{child['parent_level']}.{child['idx']}: "
                             f"need ${estimated_cost:.2f}, have ${available_usdt:.2f}")
                self._insufficient_balance_warned[strategy_name] = {
                    'key': key, 'last_warn_time': now,
                }
            return None

        ok, reason = self.client.check_percent_price_filter(symbol, 'BUY', child['buy_price'])
        if not ok:
            logger.debug(f"[{strategy_name}] Skipping child L{child['parent_level']}.{child['idx']}: {reason}")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol,
                side='BUY',
                quantity=child['qty'],
                price=child['buy_price']
            )
            self.active_orders[order['orderId']] = {
                'strategy': strategy_name,
                'level': child['parent_level'],
                'type': 'BUY',
                'order': order,
                'ladder': parent_ladder,
                'child': child,
                'placed_at': time.time(),
            }
            child['status'] = 'placed'
            parent_ladder['children_placed'] = parent_ladder.get('children_placed', 0) + 1
            self._insufficient_balance_warned.pop(strategy_name, None)
            logger.info(f"[{strategy_name}] Child BUY placed: L{child['parent_level']}.{child['idx']} "
                       f"@ ${child['buy_price']:.4f} ({child['qty']:.6f}, ${estimated_cost:.2f})")
            return order
        except Exception as e:
            logger.error(f"[{strategy_name}] Failed to place child BUY "
                        f"L{child['parent_level']}.{child['idx']}: {e}")
            return None

    def _place_child_sell(self, strategy, child, parent_ladder, filled_qty,
                          current_price=None):
        """Place a SELL for a freshly-filled child.

        Two paths:
          1. Recovery path — when recovery is enabled and the price is already
             well below the child's buy price (drawdown_threshold breached),
             the child is pooled into a per-strategy recovery lot. Once
             min_merge_count children are pooled, they are sold together as
             one merged SELL targeting avg_cost * (1 + recovery_profit).
          2. Normal path (legacy) — the child gets its own SELL at its
             precomputed sell_price (child_profit_percent over its buy).

        Both paths can run for different children at the same time, which is
        the point: small lots near the market continue to trade actively
        while underwater lots wait pooled for a bounce.
        """
        rec_cfg = strategy.get_recovery_config()
        if rec_cfg['enabled'] and current_price is not None and self._eligible_for_recovery(
                child, current_price, rec_cfg):
            return self._add_to_recovery(strategy, child, parent_ladder, filled_qty,
                                         current_price, rec_cfg)
        return self._place_normal_child_sell(strategy, child, parent_ladder, filled_qty)

    def _place_normal_child_sell(self, strategy, child, parent_ladder, filled_qty):
        """Place a single child SELL at its precomputed sell_price.

        Falls back to the parent ladder's sell_price for legacy state files
        where the child dict was created before per-child sells existed.
        """
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        sell_price = child.get('sell_price') or parent_ladder['sell_price']
        try:
            order = self.client.create_limit_order(
                symbol=symbol,
                side='SELL',
                quantity=filled_qty,
                price=sell_price
            )
            self.active_orders[order['orderId']] = {
                'strategy': strategy_name,
                'level': child['parent_level'],
                'type': 'SELL',
                'order': order,
                'ladder': parent_ladder,
                'child': child,
            }
            child['status'] = 'active'
            logger.info(f"[{strategy_name}] Child SELL placed: L{child['parent_level']}.{child['idx']} "
                       f"@ ${sell_price:.4f}")
            return order
        except Exception as e:
            logger.error(f"[{strategy_name}] Failed to place child SELL "
                        f"L{child['parent_level']}.{child['idx']}: {e}")
            return None

    # ---- Recovery mode -----------------------------------------------------

    @staticmethod
    def _eligible_for_recovery(child, current_price, rec_cfg):
        """Check whether a freshly-filled child is underwater enough to merge.

        Drawdown is measured against the child's own buy price (the market
        moved past the limit before the limit could fill its individual SELL
        target).
        """
        buy = child.get('buy_price') or 0
        if buy <= 0 or current_price <= 0:
            return False
        drawdown = (buy - current_price) / buy
        return drawdown >= rec_cfg['drawdown_threshold']

    def _get_recovery_lot(self, strategy_name):
        """Return the recovery lot dict for a strategy, creating it if absent."""
        lot = self._recovery_lots.get(strategy_name)
        if lot is None:
            lot = {
                'children': [],
                'merged_sell_order_id': None,
                'merged_sell_price': None,
                'merged_sell_qty': None,
            }
            self._recovery_lots[strategy_name] = lot
        return lot

    def _add_to_recovery(self, strategy, child, parent_ladder, filled_qty,
                         current_price, rec_cfg):
        """Add a filled child to the recovery lot and (re)place merged SELL.

        Stamps the child with the actual filled quantity so the merged SELL
        adds up to what the bot really owns from this fill.
        """
        strategy_name = strategy.config['name']
        # Snapshot fields we need for resale; copy so later strategy edits
        # don't mutate our pooled record.
        snapshot = {
            'idx': child.get('idx'),
            'parent_level': child.get('parent_level'),
            'buy_price': child.get('buy_price'),
            'qty': float(filled_qty),
            'usdt_cost': float(filled_qty) * float(child.get('buy_price') or 0),
            'parent_ladder': parent_ladder,
        }
        lot = self._get_recovery_lot(strategy_name)
        lot['children'].append(snapshot)
        # Mark the live child so it doesn't get a normal SELL on a retry path.
        child['status'] = 'recovery'
        logger.info(f"[{strategy_name}] Recovery: pooled L{snapshot['parent_level']}."
                    f"{snapshot['idx']} qty={snapshot['qty']:.6f} @ buy ${snapshot['buy_price']:.4f} "
                    f"(price ${current_price:.4f}, lot size {len(lot['children'])})")
        self._refresh_merged_sell(strategy, lot, rec_cfg)
        return None  # No single order to return — merged SELL reflects the lot.

    def _compute_merged_target(self, lot, rec_cfg):
        """Return (total_qty, target_price) for the current recovery lot.

        Target = total_cost / total_qty * (1 + profit_target). With one child
        this is just child.buy_price * (1 + profit_target); the merging is
        what gives this any real edge over a per-child SELL.
        """
        total_qty = sum(c['qty'] for c in lot['children'])
        if total_qty <= 0:
            return 0.0, 0.0
        total_cost = sum(c['usdt_cost'] for c in lot['children'])
        avg_cost = total_cost / total_qty
        target = avg_cost * (1.0 + rec_cfg['profit_target'])
        return total_qty, target

    def _cancel_merged_sell(self, strategy, lot):
        """Cancel the currently active merged SELL (if any).

        Returns 'filled' if Binance reported the order already filled (the
        caller should let check_filled_orders process it on the next loop),
        'partial' if it is partially filled (we leave it alone; recomputing
        on a partial fill would require splitting children proportionally),
        'cancelled' if successfully cancelled, or 'absent' if there was no
        live merged SELL.
        """
        order_id = lot.get('merged_sell_order_id')
        if order_id is None:
            return 'absent'
        symbol = strategy.config['pair']
        try:
            status = self.client.get_order(symbol=symbol, order_id=order_id)
        except Exception as e:
            logger.warning(f"[{strategy.config['name']}] Recovery: cannot query merged SELL "
                          f"{order_id}: {e}; not replacing")
            return 'unknown'
        s = status.get('status')
        if s == 'FILLED':
            logger.info(f"[{strategy.config['name']}] Recovery: merged SELL {order_id} "
                       f"already filled; deferring to check_filled_orders")
            return 'filled'
        if s == 'PARTIALLY_FILLED':
            logger.warning(f"[{strategy.config['name']}] Recovery: merged SELL {order_id} "
                          f"is partially filled; not replacing until it resolves")
            return 'partial'
        if s in ('NEW', 'PENDING_CANCEL'):
            try:
                self.client.cancel_order(symbol=symbol, order_id=order_id)
            except Exception as e:
                logger.warning(f"[{strategy.config['name']}] Recovery: cancel failed for "
                              f"merged SELL {order_id}: {e}")
                return 'unknown'
            self.active_orders.pop(order_id, None)
            lot['merged_sell_order_id'] = None
            lot['merged_sell_price'] = None
            lot['merged_sell_qty'] = None
            return 'cancelled'
        # Anything else (CANCELED, EXPIRED, REJECTED): treat as gone.
        self.active_orders.pop(order_id, None)
        lot['merged_sell_order_id'] = None
        lot['merged_sell_price'] = None
        lot['merged_sell_qty'] = None
        return 'cancelled'

    def _refresh_merged_sell(self, strategy, lot, rec_cfg):
        """Cancel any existing merged SELL and replace it with one sized to
        the current pool. If the existing SELL is partially filled we abort —
        the partial fill will resolve through the normal check loop and we'll
        rebuild on the next add or stale scan.
        """
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']

        # Don't bother placing a merged SELL until at least min_merge_count
        # children have pooled. Below that, place an individual SELL at the
        # recovery target so the lot still earns a small bounce profit.
        if len(lot['children']) < rec_cfg['min_merge_count']:
            return self._refresh_individual_recovery_sell(strategy, lot, rec_cfg)

        cancel_status = self._cancel_merged_sell(strategy, lot)
        if cancel_status in ('partial', 'filled', 'unknown'):
            return None

        # Also clear any individual recovery SELLs (placed when the lot only
        # had one child) — they're being superseded by the merged SELL.
        self._cancel_individual_recovery_sells(strategy, lot)

        total_qty, target_price = self._compute_merged_target(lot, rec_cfg)
        if total_qty <= 0 or target_price <= 0:
            return None

        ok, reason = self.client.check_percent_price_filter(symbol, 'SELL', target_price)
        if not ok:
            logger.warning(f"[{strategy_name}] Recovery: merged SELL @ ${target_price:.4f} "
                          f"rejected by price filter: {reason}; will retry on next add")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol, side='SELL', quantity=total_qty, price=target_price
            )
        except Exception as e:
            logger.error(f"[{strategy_name}] Recovery: merged SELL placement failed: {e}")
            return None

        self.active_orders[order['orderId']] = {
            'strategy': strategy_name,
            'level': 'recovery',
            'type': 'SELL_MERGED',
            'order': order,
            'recovery_children': list(lot['children']),
        }
        lot['merged_sell_order_id'] = order['orderId']
        lot['merged_sell_price'] = float(target_price)
        lot['merged_sell_qty'] = float(total_qty)
        logger.info(f"[{strategy_name}] Recovery: merged SELL placed for "
                   f"{len(lot['children'])} children, qty={total_qty:.6f} @ ${target_price:.4f} "
                   f"(profit target {rec_cfg['profit_target']:.2%} over avg cost)")
        return order

    def _refresh_individual_recovery_sell(self, strategy, lot, rec_cfg):
        """Place individual SELL(s) for recovery children when below the merge
        threshold. Each child gets its own SELL at child.buy * (1 + target),
        registered like a normal child SELL but flagged as recovery so the
        stale-scan can roll it into a merged lot once more children join.
        """
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        existing_ids = {
            oid for oid, od in self.active_orders.items()
            if od.get('type') == 'SELL_RECOVERY' and od.get('strategy') == strategy_name
        }
        # Track which lot children already have an order so we don't double-place.
        covered_keys = set()
        for oid in existing_ids:
            ch = self.active_orders[oid].get('child') or {}
            covered_keys.add((ch.get('parent_level'), ch.get('idx')))

        for snap in lot['children']:
            key = (snap['parent_level'], snap['idx'])
            if key in covered_keys:
                continue
            target = float(snap['buy_price']) * (1.0 + rec_cfg['profit_target'])
            ok, reason = self.client.check_percent_price_filter(symbol, 'SELL', target)
            if not ok:
                logger.warning(f"[{strategy_name}] Recovery (single): SELL @ ${target:.4f} "
                              f"rejected by price filter: {reason}")
                continue
            try:
                order = self.client.create_limit_order(
                    symbol=symbol, side='SELL', quantity=snap['qty'], price=target
                )
            except Exception as e:
                logger.error(f"[{strategy_name}] Recovery (single): SELL placement failed: {e}")
                continue
            self.active_orders[order['orderId']] = {
                'strategy': strategy_name,
                'level': snap['parent_level'],
                'type': 'SELL_RECOVERY',
                'order': order,
                'ladder': snap['parent_ladder'],
                'child': {
                    'idx': snap['idx'],
                    'parent_level': snap['parent_level'],
                    'buy_price': snap['buy_price'],
                    'qty': snap['qty'],
                    'sell_price': target,
                },
            }
            logger.info(f"[{strategy_name}] Recovery (single): SELL placed L{snap['parent_level']}."
                       f"{snap['idx']} @ ${target:.4f} ({rec_cfg['profit_target']:.2%} over buy)")
        return None

    def _cancel_individual_recovery_sells(self, strategy, lot):
        """Cancel any per-child recovery SELLs for children now in the merged lot."""
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        lot_keys = {(c['parent_level'], c['idx']) for c in lot['children']}
        for oid, od in list(self.active_orders.items()):
            if od.get('type') != 'SELL_RECOVERY' or od.get('strategy') != strategy_name:
                continue
            ch = od.get('child') or {}
            if (ch.get('parent_level'), ch.get('idx')) not in lot_keys:
                continue
            try:
                self.client.cancel_order(symbol=symbol, order_id=oid)
            except Exception as e:
                logger.warning(f"[{strategy_name}] Recovery: failed to cancel individual SELL "
                              f"{oid}: {e}")
                continue
            self.active_orders.pop(oid, None)

    def check_stale_sells(self, strategy, current_price):
        """Periodically scan open SELLs whose limit price has drifted far
        above the market and roll them into the recovery lot.

        This is the second recovery trigger (the first being at-fill drawdown
        in _place_child_sell). It catches the case where a child filled in
        sideways conditions, got a normal +profit SELL, and then the market
        dropped well below it — leaving an unfillable SELL parked on the
        book. Cancelling it and re-pooling the position lets us settle for a
        smaller bounce on a combined cost basis.

        Throttled by stale_check_interval_seconds.
        """
        rec_cfg = strategy.get_recovery_config()
        if not rec_cfg['enabled']:
            return 0
        strategy_name = strategy.config['name']
        now = time.time()
        last = self._last_stale_check.get(strategy_name, 0)
        if now - last < rec_cfg['stale_check_interval_seconds']:
            return 0
        self._last_stale_check[strategy_name] = now

        threshold = rec_cfg['stale_sell_threshold']
        if threshold <= 0 or current_price <= 0:
            return 0

        lot = self._get_recovery_lot(strategy_name)
        rolled = 0
        symbol = strategy.config['pair']

        for oid, od in list(self.active_orders.items()):
            if od.get('strategy') != strategy_name:
                continue
            otype = od.get('type')
            # Only normal child SELLs and individual recovery SELLs are
            # candidates. SELL_MERGED is itself the recovery exit, so leave
            # it alone.
            if otype not in ('SELL', 'SELL_RECOVERY'):
                continue
            order_obj = od.get('order') or {}
            try:
                sell_price = float(order_obj.get('price') or 0)
            except (TypeError, ValueError):
                continue
            if sell_price <= 0:
                continue
            distance = (sell_price - current_price) / current_price
            if distance < threshold:
                continue

            child = od.get('child') or {}
            qty_str = order_obj.get('origQty') or order_obj.get('executedQty') or child.get('qty')
            try:
                qty = float(qty_str) if qty_str is not None else float(child.get('qty', 0))
            except (TypeError, ValueError):
                qty = float(child.get('qty', 0))
            if qty <= 0:
                continue

            try:
                self.client.cancel_order(symbol=symbol, order_id=oid)
            except Exception as e:
                logger.warning(f"[{strategy_name}] Recovery: cancel of stale SELL {oid} failed: {e}")
                continue
            self.active_orders.pop(oid, None)

            buy_price = float(child.get('buy_price') or 0)
            if buy_price <= 0:
                # Fallback: derive a buy price from the SELL using the original
                # child profit so the recovery cost basis is at least sensible.
                placement = strategy.config.get('order_placement', {})
                cp_pct = float(placement.get('child_profit_percent', 0.012) or 0.012)
                buy_price = sell_price / (1.0 + cp_pct)

            snapshot = {
                'idx': child.get('idx'),
                'parent_level': child.get('parent_level', od.get('level')),
                'buy_price': buy_price,
                'qty': qty,
                'usdt_cost': qty * buy_price,
                'parent_ladder': od.get('ladder'),
            }
            lot['children'].append(snapshot)
            rolled += 1
            logger.info(f"[{strategy_name}] Recovery: stale SELL {oid} L{snapshot['parent_level']}"
                       f".{snapshot['idx']} @ ${sell_price:.4f} cancelled "
                       f"(price ${current_price:.4f}, distance {distance:.2%}); pooled into recovery")

        # Also re-place a merged SELL for any recovery lot that has children
        # but no live order (e.g. it got cancelled on the exchange, or we
        # restarted with a populated lot but the order is gone). Otherwise
        # the lot would sit there waiting forever.
        live_merged_id = lot.get('merged_sell_order_id')
        merged_alive = live_merged_id is not None and live_merged_id in self.active_orders
        if rolled > 0 or (lot['children'] and not merged_alive):
            self._refresh_merged_sell(strategy, lot, rec_cfg)
        return rolled

    def _drop_from_recovery_lot(self, strategy_name, parent_level, idx):
        """Remove a child from the per-strategy recovery lot once its
        individual recovery SELL has filled."""
        lot = self._recovery_lots.get(strategy_name)
        if not lot:
            return
        lot['children'] = [
            c for c in lot['children']
            if not (c.get('parent_level') == parent_level and c.get('idx') == idx)
        ]
        if not lot['children']:
            self._recovery_lots.pop(strategy_name, None)

    def _handle_merged_sell_fill(self, order_data, status):
        """Distribute a merged-SELL fill across all participating children.

        Each child's portfolio position is closed at the merged sell price,
        weighted by the child's own quantity. The parent-ladder children_closed
        counters are bumped, and once a ladder has all its children closed the
        ladder itself is marked closed (matching the per-child SELL flow).
        """
        strategy_name = order_data['strategy']
        children = order_data.get('recovery_children') or []
        sell_price = float(status.get('price') or 0)
        executed_qty = float(status.get('executedQty') or 0)
        total_pool = sum(float(c.get('qty', 0)) for c in children)
        # Defensive: scale child qty to actual fill if Binance rounded.
        scale = (executed_qty / total_pool) if total_pool > 0 else 1.0
        for snap in children:
            qty = float(snap.get('qty', 0)) * scale
            if qty <= 0:
                continue
            level_tuple = (snap.get('parent_level'), snap.get('idx'))
            self.portfolio.close_position(
                strategy_name=strategy_name,
                ladder_level=level_tuple,
                sell_price=sell_price,
                quantity=qty,
            )
            parent = snap.get('parent_ladder') or {}
            parent['children_closed'] = parent.get('children_closed', 0) + 1
            if parent.get('children_total', 0) > 0 \
                    and parent['children_closed'] >= parent['children_total']:
                parent['status'] = 'closed'
                # Same in-place recycle hook as the normal SELL close path.
                if self._recycle_in_place_enabled(strategy_name):
                    self._recycle_ladder_in_place(strategy_name, parent)
        logger.info(f"[{strategy_name}] Recovery: merged SELL filled @ ${sell_price:.4f}, "
                   f"{len(children)} children closed (qty {executed_qty:.6f})")
        # Recovery cycle complete — clear pool so the next drawdown starts fresh.
        self._recovery_lots.pop(strategy_name, None)

    # ---- Accumulation (SELL-then-BUY-back) --------------------------------

    def is_accumulation_enabled(self, strategy):
        """Check whether SELL-side accumulation is configured for this strategy."""
        return strategy.is_accumulation_enabled()

    def log_planned_sell_ladders(self, strategy):
        """Log all planned SELL accumulation ladders so user sees the plan."""
        if not strategy.is_accumulation_enabled():
            return
        strategy_name = strategy.config['name']
        if not strategy.sell_ladders:
            return
        accum_cfg = strategy.get_accumulation_config()
        logger.info(f"[{strategy_name}] Planned SELL (accumulation) ladders "
                    f"(coin_profit_percent={accum_cfg['coin_profit_percent']:.2%}, "
                    f"proximity={accum_cfg['proximity_percent']:.2%}):")
        for ladder in strategy.sell_ladders:
            sell_price = ladder.get('sell_price', 0)
            buyback_price = ladder.get('buyback_price', 0)
            coin_amount = ladder.get('coin_amount', 0)
            expected_gain = ladder.get('expected_coin_gain', 0)
            tier = ladder.get('tier', 'fib')
            logger.info(f"  Lvl +{ladder['level']:>2} [{tier:>5}]: "
                       f"SELL @ ${sell_price:>10.2f} | "
                       f"BUYBACK @ ${buyback_price:>10.2f} | "
                       f"Sell Qty: {coin_amount:.4f} ({ladder['units']}x) | "
                       f"Coin Gain: +{expected_gain:.6f}")

    def place_accumulation_orders(self, strategy, current_price):
        """Promote pending SELL accumulation ladders whose price is in range.

        Mirrors place_distribution_orders: only places SELL when the market
        price is within proximity_percent below the ladder's sell price (or
        already at/above it). Hard-fails (raises) when coin balance is
        insufficient so the user gets a clear error in logs.

        Returns list of orders placed this call (may be empty).
        """
        if not strategy.is_accumulation_enabled():
            return []

        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        accum_cfg = strategy.get_accumulation_config()
        proximity = accum_cfg['proximity_percent']
        cap = accum_cfg['max_open_sells_cap']

        pending = strategy.get_pending_sell_ladders()
        if not pending:
            return []

        # Cap on simultaneous accumulation SELLs (counts only SELL_ACCUM and
        # BUY_BACK from THIS strategy so it doesn't fight with the BUY-side
        # distribution cap).
        active_accum = sum(
            1 for od in self.active_orders.values()
            if od.get('strategy') == strategy_name
            and od.get('type') in ('SELL_ACCUM', 'BUY_BACK')
        )

        # If any pending ladder is already waiting on balance (or marked
        # stuck), don't promote ladders ABOVE it — they'd just queue up
        # competing for the same insufficient balance and spam the log.
        sorted_pending = sorted(pending, key=lambda l: l['sell_price'])
        block_above_price = None
        for l in sorted_pending:
            if l.get('waiting_since') is not None or l.get('stuck_at_balance') is not None:
                block_above_price = l['sell_price']
                break

        placed = []
        for ladder in sorted_pending:
            if active_accum >= cap:
                logger.debug(f"[{strategy_name}] Accumulation cap ({cap}) reached, "
                             f"{len(pending) - len(placed)} sell ladders still pending")
                break

            sell_price = ladder.get('sell_price', 0)
            if sell_price <= 0:
                continue

            if block_above_price is not None and sell_price > block_above_price:
                # Lower ladder is still waiting on balance; defer higher ones.
                continue

            # Promote when price is at/above sell price OR within proximity below it.
            if current_price >= sell_price:
                should_promote = True
            else:
                distance = (sell_price - current_price) / sell_price
                should_promote = distance <= proximity

            if not should_promote:
                # Sorted bottom-first; if this one isn't in range, higher
                # ones definitely aren't either.
                break

            order = self._place_accumulation_sell(strategy, ladder)
            if order is not None:
                placed.append(order)
                active_accum += 1

        return placed

    def _clear_stuck_sell_ladders(self, strategy_name):
        """Drop stuck-balance watermarks for a strategy's sell ladders.

        Called after events that grow the coin balance (e.g. a BUY fill) so
        the next placement cycle re-evaluates ladders that had been deferred.
        """
        strategy = self._strategy_by_name(strategy_name)
        if strategy is None:
            return
        for ladder in getattr(strategy, 'sell_ladders', []) or []:
            ladder.pop('stuck_at_balance', None)
            ladder.pop('stuck_warned_at', None)

    # ------------------------------------------------------------------
    # Inventory Hoard (mini-scalper) — isolated micro BUY -> SELL layer
    # ------------------------------------------------------------------
    def _hoard_get_state(self, strategy_name):
        return self._hoard_state.setdefault(strategy_name, {
            'budget_used_usdt': 0.0,
            'recent_buy_ts': [],
            'last_buy_ts': 0.0,
        })

    def _hoard_count_open(self, strategy_name):
        return sum(
            1 for od in self.active_orders.values()
            if od.get('strategy') == strategy_name
            and od.get('type') in ('HOARD_BUY', 'HOARD_SELL')
        )

    def _hoard_first_stuck_ladder(self, strategy, trigger_level):
        """Return the lowest-level stuck SELL accum ladder at/below the trigger
        threshold, or None."""
        for ladder in getattr(strategy, 'sell_ladders', []) or []:
            if ladder.get('level', 999) > trigger_level:
                continue
            if ladder.get('stuck_at_balance') is not None:
                return ladder
        return None

    def tick_inventory_hoard(self, strategy, current_price):
        """Place a micro hoard BUY when main SELL accum is stuck and price
        has rallied past the stuck level. Fully isolated from the main
        BUY/SELL ladders: separate budget, separate stats, separate tags.
        """
        cfg = strategy.get_inventory_hoard_config()
        if not cfg['enabled']:
            return None

        stuck_ladder = self._hoard_first_stuck_ladder(strategy, cfg['trigger_on_stuck_level'])
        if stuck_ladder is None:
            return None

        sell_price = float(stuck_ladder.get('sell_price') or 0)
        if sell_price <= 0:
            return None
        threshold = sell_price * (1.0 + cfg['trigger_price_above_pct'])
        if current_price < threshold:
            return None

        strategy_name = strategy.config['name']
        state = self._hoard_get_state(strategy_name)
        now = time.time()

        # Cooldown
        if now - state['last_buy_ts'] < cfg['cooldown_seconds']:
            return None
        # Open-order cap
        if self._hoard_count_open(strategy_name) >= cfg['max_open_orders']:
            return None
        # Per-hour rate limit
        state['recent_buy_ts'] = [t for t in state['recent_buy_ts'] if now - t < 3600]
        if len(state['recent_buy_ts']) >= cfg['max_per_hour']:
            return None
        # Budget cap — recompute from live HOARD_BUY orders so that orders
        # adopted from state on restart are accounted for correctly.
        live_locked = sum(
            float((od.get('hoard') or {}).get('locked_usdt') or 0.0)
            for od in self.active_orders.values()
            if od.get('strategy') == strategy_name and od.get('type') == 'HOARD_BUY'
        )
        state['budget_used_usdt'] = live_locked
        order_usdt = cfg['child_order_usdt']
        if state['budget_used_usdt'] + order_usdt > cfg['hoard_budget_usdt']:
            return None

        symbol = strategy.config['pair']
        # Use a slightly aggressive limit BUY (just above current price) so
        # it fills quickly without paying full taker on a market order. If
        # price keeps running we miss this one — that's intended; we'll
        # try again next tick after cooldown.
        buy_price = current_price * 1.0005
        qty = order_usdt / buy_price

        try:
            filters = self.client.get_symbol_filters(symbol)
            min_qty = float(filters.get('min_qty', 0) or 0)
            min_notional = float(filters.get('min_notional', 0) or 0)
        except Exception as e:
            logger.error(f"[{strategy_name}] hoard: cannot fetch filters: {e}")
            return None

        if qty < min_qty or qty * buy_price < min_notional:
            logger.warning(
                f"[{strategy_name}] hoard: child_order_usdt ${order_usdt:.2f} too small "
                f"(min_qty={min_qty}, min_notional=${min_notional:.2f}). Skipping."
            )
            return None

        ok, reason = self.client.check_percent_price_filter(symbol, 'BUY', buy_price)
        if not ok:
            logger.warning(f"[{strategy_name}] hoard BUY rejected by price filter: {reason}")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol, side='BUY', quantity=qty, price=buy_price,
            )
        except Exception as e:
            logger.error(f"[{strategy_name}] hoard BUY failed: {e}")
            return None

        # Lock budget against the actual rounded notional we submitted.
        actual_qty = float(order.get('origQty') or qty)
        actual_price = float(order.get('price') or buy_price)
        locked_usdt = actual_qty * actual_price

        self.active_orders[order['orderId']] = {
            'strategy': strategy_name,
            'level': 'HOARD',
            'type': 'HOARD_BUY',
            'order': order,
            'hoard': {
                'buy_price': actual_price,
                'qty': actual_qty,
                'locked_usdt': locked_usdt,
                'profit_percent': cfg['profit_percent'],
                'trigger_sell_price': sell_price,
            },
        }
        state['budget_used_usdt'] += locked_usdt
        state['recent_buy_ts'].append(now)
        state['last_buy_ts'] = now
        logger.info(
            f"[{strategy_name}] HOARD BUY placed: {actual_qty:.6f} @ ${actual_price:.4f} "
            f"(stuck Lvl +{stuck_ladder.get('level')} @ ${sell_price:.4f}, "
            f"price now ${current_price:.4f}, budget {state['budget_used_usdt']:.2f}/"
            f"{cfg['hoard_budget_usdt']:.2f})"
        )
        return order

    def _place_hoard_sell(self, strategy, hoard_meta, filled_qty, filled_price):
        """Place the matching HOARD_SELL right after a HOARD_BUY fills."""
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        profit = float(hoard_meta.get('profit_percent') or 0.007)
        sell_price = filled_price * (1.0 + profit)

        ok, reason = self.client.check_percent_price_filter(symbol, 'SELL', sell_price)
        if not ok:
            logger.warning(f"[{strategy_name}] HOARD SELL rejected by price filter: {reason}")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol, side='SELL', quantity=filled_qty, price=sell_price,
            )
        except Exception as e:
            logger.error(f"[{strategy_name}] HOARD SELL failed: {e}")
            return None

        self.active_orders[order['orderId']] = {
            'strategy': strategy_name,
            'level': 'HOARD',
            'type': 'HOARD_SELL',
            'order': order,
            'hoard': {
                'buy_price': filled_price,
                'sell_price': sell_price,
                'qty': filled_qty,
                'profit_percent': profit,
            },
        }
        logger.info(
            f"[{strategy_name}] HOARD SELL placed: {filled_qty:.6f} @ ${sell_price:.4f} "
            f"(target +{profit:.2%} from ${filled_price:.4f})"
        )
        return order

    def _record_hoard_cycle(self, strategy_name, hoard_meta, sold_qty, sold_price):
        """Record completed HOARD round-trip and free its budget slot."""
        buy_price = float(hoard_meta.get('buy_price') or 0)
        usdt_gain = (sold_price - buy_price) * sold_qty
        stats = self._hoard_stats.setdefault(strategy_name, {
            'usdt_gain_total': 0.0, 'cycles_completed': 0,
        })
        stats['usdt_gain_total'] += usdt_gain
        stats['cycles_completed'] += 1
        # Free budget that was locked at BUY time. Use the original locked
        # notional if available; fall back to current sold notional.
        state = self._hoard_get_state(strategy_name)
        locked = float(hoard_meta.get('locked_usdt') or buy_price * sold_qty)
        state['budget_used_usdt'] = max(0.0, state['budget_used_usdt'] - locked)
        logger.info(
            f"[{strategy_name}] HOARD cycle done: +${usdt_gain:.4f} "
            f"(buy ${buy_price:.4f} -> sell ${sold_price:.4f}, qty {sold_qty:.6f}). "
            f"Total: ${stats['usdt_gain_total']:.4f} over {stats['cycles_completed']} cycles."
        )

    def _place_accumulation_sell(self, strategy, ladder):
        """Place one SELL_ACCUM order.

        Insufficient coin balance no longer crashes the bot. Instead we mark
        the ladder as waiting and retry on subsequent cycles. If the wait
        exceeds `wait_for_balance_seconds`, we downsize the order to the
        coins actually available (subject to LOT_SIZE / NOTIONAL filters)
        so the bot can still participate in the move rather than miss it.
        """
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        base_asset = symbol.replace('USDT', '').replace('BUSD', '').replace('USDC', '')

        sell_price = ladder['sell_price']
        coin_amount = ladder['coin_amount']

        try:
            balance = self.client.get_account_balance(base_asset)
            available = balance['free']
        except Exception as e:
            logger.error(f"[{strategy_name}] Cannot fetch {base_asset} balance: {e}")
            return None

        accum_cfg = strategy.get_accumulation_config()
        reserve = accum_cfg['reserve_coin_percent']
        usable = available * (1.0 - reserve)
        wait_seconds = accum_cfg.get('wait_for_balance_seconds', 600)
        allow_partial = accum_cfg.get('allow_partial_after_wait', True)

        # If this ladder was previously marked "stuck" (usable balance too
        # small for even a partial SELL), stay silent until balance actually
        # grows past the recorded watermark. Avoids flooding the log every
        # poll while we wait for a BUY to refill the coin balance.
        stuck_at = ladder.get('stuck_at_balance')
        if stuck_at is not None and usable <= stuck_at + 1e-12:
            return None
        if stuck_at is not None and usable > stuck_at:
            ladder.pop('stuck_at_balance', None)
            ladder.pop('stuck_warned_at', None)

        downsized = False
        if coin_amount > usable:
            now = time.time()
            waiting_since = ladder.get('waiting_since')
            if waiting_since is None:
                ladder['waiting_since'] = now
                waiting_since = now
            elapsed = now - waiting_since

            # Throttle the warning so logs don't spam every poll.
            warn_state = self._insufficient_balance_warned.setdefault(strategy_name, {})
            last_warn = warn_state.get(ladder['level'], 0)
            if now - last_warn >= 60:
                logger.warning(
                    f"[{strategy_name}] Waiting for {base_asset} balance for SELL accum "
                    f"Lvl +{ladder['level']}: need {coin_amount:.6f}, usable "
                    f"{usable:.6f} of {available:.6f} (reserve {reserve:.0%}). "
                    f"Waited {elapsed:.0f}s / {wait_seconds:.0f}s before adjusting."
                )
                warn_state[ladder['level']] = now

            if not allow_partial or elapsed < wait_seconds:
                # Still inside the grace window — keep waiting for a dip /
                # for upstream BUYs to fill and replenish the coin balance.
                return None

            # Grace window elapsed: downsize to what's actually available so
            # the bot doesn't sit out the entire move. Respect step_size and
            # min_notional; if neither fits, defer for another cycle.
            try:
                filters = self.client.get_symbol_filters(symbol)
                step_size = filters.get('step_size', '0.001')
                min_qty = float(filters.get('min_qty', 0) or 0)
                min_notional = float(filters.get('min_notional', 0) or 0)
            except Exception as e:
                logger.error(f"[{strategy_name}] Cannot fetch filters for partial SELL: {e}")
                return None

            adjusted_qty = float(self.client._round_to_step(usable, step_size))
            if adjusted_qty <= 0 or adjusted_qty < min_qty or adjusted_qty * sell_price < min_notional:
                # Mark the ladder as stuck so future polls stay silent until
                # the balance actually grows (BUY fills clear this watermark
                # via _clear_stuck_sell_ladders).
                if ladder.get('stuck_at_balance') != usable:
                    logger.warning(
                        f"[{strategy_name}] Lvl +{ladder['level']}: usable balance "
                        f"{usable:.6f} {base_asset} too small to place a partial SELL "
                        f"(min_qty={min_qty}, min_notional=${min_notional:.2f}). "
                        f"Deferring until balance grows."
                    )
                    ladder['stuck_at_balance'] = usable
                    ladder['stuck_warned_at'] = now
                return None

            logger.warning(
                f"[{strategy_name}] Lvl +{ladder['level']}: downsizing SELL from "
                f"{coin_amount:.6f} to {adjusted_qty:.6f} {base_asset} after waiting "
                f"{elapsed:.0f}s for balance."
            )
            coin_amount = adjusted_qty
            downsized = True

        ok, reason = self.client.check_percent_price_filter(symbol, 'SELL', sell_price)
        if not ok:
            logger.warning(f"[{strategy_name}] Skipping accum SELL Lvl +{ladder['level']}: {reason}")
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol, side='SELL',
                quantity=coin_amount, price=sell_price,
            )
        except Exception as e:
            logger.error(f"[{strategy_name}] Failed to place accum SELL Lvl +{ladder['level']}: {e}")
            return None

        self.active_orders[order['orderId']] = {
            'strategy': strategy_name,
            'level': ladder['level'],
            'type': 'SELL_ACCUM',
            'order': order,
            'sell_ladder': ladder,
        }
        ladder['status'] = 'placed'
        if downsized:
            # Record so buyback sizing reflects what we actually sold.
            ladder['placed_coin_amount'] = coin_amount
        ladder.pop('waiting_since', None)
        ladder.pop('stuck_at_balance', None)
        ladder.pop('stuck_warned_at', None)
        warn_state = self._insufficient_balance_warned.get(strategy_name)
        if warn_state is not None:
            warn_state.pop(ladder['level'], None)
        logger.info(f"[{strategy_name}] Accum SELL placed: Lvl +{ladder['level']} "
                   f"@ ${sell_price:.4f} qty={coin_amount:.6f}"
                   f"{' [partial]' if downsized else ''} (target buyback "
                   f"@ ${ladder['buyback_price']:.4f}, +{strategy.get_accumulation_config()['coin_profit_percent']:.2%} coins)")
        return order

    def _place_buyback_buy(self, strategy, sell_ladder, filled_qty, filled_sell_price):
        """Place BUY_BACK after a SELL_ACCUM filled.

        Buyback price = MIN(structural buyback, sell_price / (1 + coin_profit))
        — the cap guarantees the buyback yields more coins than were sold.
        Buyback qty = filled_qty × (sell_price / buyback_price), so the
        coin gain ≈ filled_qty × coin_profit_percent (or better).

        Returns the placed BUY order or None on failure.
        """
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        accum_cfg = strategy.get_accumulation_config()
        coin_profit = accum_cfg['coin_profit_percent']

        # Always respect the profit cap, even if structural buyback is lower
        # (deeper levels). We want at minimum +coin_profit% in coins back.
        cap_price = filled_sell_price / (1.0 + coin_profit)
        structural = float(sell_ladder.get('buyback_price') or cap_price)
        buyback_price = min(structural, cap_price)
        if buyback_price <= 0:
            logger.error(f"[{strategy_name}] Invalid buyback price for Lvl +{sell_ladder['level']}; "
                        f"sell_price={filled_sell_price}, cap={cap_price}, structural={structural}")
            return None

        # USDT proceeds from the SELL → buy back as many coins as that
        # amount lets us at the buyback price. This is what locks in the
        # coin gain.
        usdt_proceeds = filled_qty * filled_sell_price
        buyback_qty = usdt_proceeds / buyback_price

        ok, reason = self.client.check_percent_price_filter(symbol, 'BUY', buyback_price)
        if not ok:
            logger.warning(f"[{strategy_name}] BUYBACK Lvl +{sell_ladder['level']} rejected by "
                          f"price filter: {reason}; will retry on next stale scan")
            sell_ladder['status'] = 'awaiting_buyback'
            sell_ladder['pending_buyback'] = {
                'qty_to_recover': buyback_qty,
                'sold_qty': filled_qty,
                'sold_price': filled_sell_price,
            }
            return None

        try:
            order = self.client.create_limit_order(
                symbol=symbol, side='BUY',
                quantity=buyback_qty, price=buyback_price,
            )
        except Exception as e:
            logger.error(f"[{strategy_name}] BUYBACK placement failed Lvl +{sell_ladder['level']}: {e}")
            sell_ladder['status'] = 'awaiting_buyback'
            sell_ladder['pending_buyback'] = {
                'qty_to_recover': buyback_qty,
                'sold_qty': filled_qty,
                'sold_price': filled_sell_price,
            }
            return None

        self.active_orders[order['orderId']] = {
            'strategy': strategy_name,
            'level': sell_ladder['level'],
            'type': 'BUY_BACK',
            'order': order,
            'sell_ladder': sell_ladder,
            'sold_qty': filled_qty,
            'sold_price': filled_sell_price,
            'expected_buyback_qty': buyback_qty,
        }
        sell_ladder['status'] = 'awaiting_buyback'
        sell_ladder['pending_buyback'] = None
        expected_gain = buyback_qty - filled_qty
        logger.info(f"[{strategy_name}] BUYBACK placed: Lvl +{sell_ladder['level']} "
                   f"BUY {buyback_qty:.6f} @ ${buyback_price:.4f} "
                   f"(sold {filled_qty:.6f} @ ${filled_sell_price:.4f}, "
                   f"target coin gain +{expected_gain:.6f})")
        return order

    def _record_buyback_fill(self, strategy_name, sell_ladder, sold_qty, bought_qty,
                             sold_price=0.0, bought_price=0.0):
        """Update accumulation stats after a BUY_BACK fills.

        Reports both axes of profit on every cycle:
          - coin_gain  = bought_qty - sold_qty   (extra ZEC kept per cycle)
          - usdt_gain  = sold_qty*sold_price - bought_qty*bought_price
                         (extra USDT kept; positive whenever sell_price >
                         buyback_price, even when coin_gain rounds to 0)

        With small lots, step_size rounding usually pulls bought_qty back to
        sold_qty so coin_gain prints as 0. The USDT gain stays positive,
        which is the real profit captured by the accumulation leg.
        """
        stats = self._accumulation_stats.setdefault(strategy_name, {
            'coin_gain_total': 0.0,
            'cycles_completed': 0,
            'usdt_gain_total': 0.0,
        })
        # Backfill for old state files written before usdt tracking existed.
        stats.setdefault('usdt_gain_total', 0.0)
        coin_gain = bought_qty - sold_qty
        usdt_gain = (sold_qty * sold_price) - (bought_qty * bought_price)
        stats['coin_gain_total'] += coin_gain
        stats['usdt_gain_total'] += usdt_gain
        stats['cycles_completed'] += 1
        sell_ladder['status'] = 'closed'
        sell_ladder['last_coin_gain'] = coin_gain
        sell_ladder['last_usdt_gain'] = usdt_gain
        logger.info(
            f"[{strategy_name}] Accumulation cycle #{stats['cycles_completed']} "
            f"complete: sold {sold_qty:.6f} @ ${sold_price:.4f} → "
            f"bought {bought_qty:.6f} @ ${bought_price:.4f} | "
            f"coin gain +{coin_gain:.6f} (cum +{stats['coin_gain_total']:.6f}) | "
            f"usdt gain ${usdt_gain:+.4f} (cum ${stats['usdt_gain_total']:+.4f})"
        )

    def reset_accumulation_state(self, strategy_name):
        """Clear accumulation state for a strategy (e.g. on full reset)."""
        self._accumulation_stats.pop(strategy_name, None)

    def log_planned_ladders(self, strategy):
        """Log all planned ladder levels with prices so user knows what will be placed."""
        strategy_name = strategy.config['name']
        pending = strategy.get_pending_ladders()
        if not pending:
            return

        logger.info(f"[{strategy_name}] Planned ladder levels (orders will be placed one at a time as price approaches):")
        for ladder in pending:
            buy_price = ladder.get('buy_price', 0)
            sell_price = ladder.get('sell_price', 0)
            usdt_cost = ladder.get('usdt_cost', 0)
            units = ladder.get('units', 0)
            amount = ladder.get('btc_amount', 0)
            logger.info(f"  Level {ladder['level']:>3}: BUY @ ${buy_price:>10.2f} | "
                       f"SELL @ ${sell_price:>10.2f} | "
                       f"Cost: ${usdt_cost:>10.2f} | "
                       f"Qty: {amount:.4f} ({units}x)")

    def place_sell_order(self, strategy, ladder):
        """Place sell order for an active ladder"""
        try:
            order = self.client.create_limit_order(
                symbol=strategy.config['pair'],
                side='SELL',
                quantity=ladder['btc_amount'],
                price=ladder['sell_price']
            )

            self.active_orders[order['orderId']] = {
                'strategy': strategy.config['name'],
                'level': ladder['level'],
                'type': 'SELL',
                'order': order,
                'ladder': ladder
            }

            logger.info(f"Sell order placed: Level {ladder['level']} @ ${ladder['sell_price']:.2f}")
            return order

        except Exception as e:
            logger.error(f"Failed to place sell order for level {ladder['level']}: {e}")
            return None

    def check_filled_orders(self):
        """Check which orders have been filled"""
        filled_orders = []

        for order_id, order_data in list(self.active_orders.items()):
            try:
                # Query order status
                status = self.client.get_order(
                    symbol=order_data['order']['symbol'],
                    order_id=order_id,
                )

                if status['status'] == 'FILLED':
                    order_data['filled_price'] = float(status['price'])
                    order_data['filled_qty'] = float(status['executedQty'])
                    filled_orders.append(order_data)

                    otype = order_data['type']
                    child = order_data.get('child')
                    # Distribution children use a composite level for portfolio
                    # bookkeeping so multiple children per ladder don't collide.
                    if otype == 'SELL_MERGED':
                        position_level = None  # handled by _handle_merged_sell_fill
                    elif otype in ('SELL_ACCUM', 'BUY_BACK', 'HOARD_BUY', 'HOARD_SELL'):
                        position_level = None  # handled inline below
                    else:
                        position_level = (
                            (order_data['level'], child['idx']) if child
                            else order_data['level']
                        )

                    if otype == 'SELL_ACCUM':
                        # Accumulation SELL filled: trigger BUY_BACK placement.
                        # Note: we don't update Portfolio here — accumulation
                        # operates on coins already in the wallet, not on
                        # the BUY-ladder's tracked positions. P&L is tracked
                        # in coin units via _accumulation_stats.
                        sell_ladder = order_data.get('sell_ladder') or {}
                        filled_price = float(status.get('price') or sell_ladder.get('sell_price') or 0)
                        filled_qty = float(status.get('executedQty') or sell_ladder.get('coin_amount') or 0)
                        sell_ladder['filled_price'] = filled_price
                        sell_ladder['filled_qty'] = filled_qty
                        logger.info(f"[{order_data['strategy']}] Accum SELL filled: "
                                   f"Lvl +{order_data['level']} {filled_qty:.6f} @ ${filled_price:.4f}")
                        # Defer the buyback placement — main loop will inspect
                        # `filled` and call _place_buyback_buy with the live
                        # strategy reference (we don't have it here).
                        order_data['needs_buyback'] = True

                    elif otype == 'BUY_BACK':
                        sell_ladder = order_data.get('sell_ladder') or {}
                        bought_qty = float(status.get('executedQty') or 0)
                        sold_qty = float(order_data.get('sold_qty') or 0)
                        sold_price = float(order_data.get('sold_price') or 0)
                        # Buyback's actual fill price (Binance returns the
                        # limit price string in `price`).
                        try:
                            bought_price = float(status.get('price') or 0)
                        except (TypeError, ValueError):
                            bought_price = 0.0
                        self._record_buyback_fill(
                            order_data['strategy'], sell_ladder,
                            sold_qty=sold_qty, bought_qty=bought_qty,
                            sold_price=sold_price, bought_price=bought_price,
                        )
                        # Per-ladder recycle keeps the SELL leg of accumulation
                        # active without dragging the BUY leg through a global
                        # auto-restart (which would re-anchor the entire fib
                        # tier to the new market price).
                        if self._recycle_in_place_enabled(order_data.get('strategy')):
                            self._recycle_sell_ladder_in_place(
                                order_data['strategy'], sell_ladder
                            )

                    elif otype == 'HOARD_BUY':
                        hoard_meta = order_data.get('hoard') or {}
                        filled_price = float(status.get('price') or hoard_meta.get('buy_price') or 0)
                        filled_qty = float(status.get('executedQty') or hoard_meta.get('qty') or 0)
                        hoard_meta['filled_price'] = filled_price
                        hoard_meta['filled_qty'] = filled_qty
                        logger.info(
                            f"[{order_data['strategy']}] HOARD BUY filled: "
                            f"{filled_qty:.6f} @ ${filled_price:.4f}"
                        )
                        # Place HOARD_SELL synchronously so the new coins are
                        # locked on the exchange before the main accumulation
                        # tick can sweep them into a SELL_ACCUM.
                        strat = self._strategy_by_name(order_data['strategy'])
                        if strat is not None:
                            self._place_hoard_sell(
                                strat, hoard_meta,
                                filled_qty=filled_qty,
                                filled_price=filled_price,
                            )

                    elif otype == 'HOARD_SELL':
                        hoard_meta = order_data.get('hoard') or {}
                        filled_qty = float(status.get('executedQty') or hoard_meta.get('qty') or 0)
                        try:
                            filled_price = float(status.get('price') or hoard_meta.get('sell_price') or 0)
                        except (TypeError, ValueError):
                            filled_price = float(hoard_meta.get('sell_price') or 0)
                        self._record_hoard_cycle(
                            order_data['strategy'], hoard_meta,
                            sold_qty=filled_qty, sold_price=filled_price,
                        )

                    elif otype == 'BUY':
                        self.portfolio.add_position(
                            strategy_name=order_data['strategy'],
                            ladder_level=position_level,
                            buy_price=float(status['price']),
                            quantity=float(status['executedQty']),
                            cost=float(status['cummulativeQuoteQty'])
                        )
                        order_data['ladder']['status'] = 'active'

                        # Coin balance just grew — let any sell ladders that
                        # were marked stuck reassess on the next cycle.
                        self._clear_stuck_sell_ladders(order_data['strategy'])

                        if child is not None:
                            logger.info(f"Child BUY filled: "
                                       f"L{order_data['level']}.{child['idx']}")
                        else:
                            logger.info(f"Buy order filled: Level {order_data['level']}")

                    elif otype == 'SELL_MERGED':
                        # Recovery exit — fan out the fill to every pooled child.
                        self._handle_merged_sell_fill(order_data, status)

                    elif otype in ('SELL', 'SELL_RECOVERY'):
                        self.portfolio.close_position(
                            strategy_name=order_data['strategy'],
                            ladder_level=position_level,
                            sell_price=float(status['price']),
                            quantity=float(status['executedQty'])
                        )

                        if child is not None:
                            child['status'] = 'closed'
                            parent = order_data.get('ladder') or {}
                            parent['children_closed'] = parent.get('children_closed', 0) + 1
                            # Mark the whole ladder closed only when every child
                            # has completed its cycle.
                            ladder_just_closed = False
                            if parent.get('children_total', 0) > 0 \
                                    and parent['children_closed'] >= parent['children_total']:
                                parent['status'] = 'closed'
                                ladder_just_closed = True
                            tag = "Child SELL" if otype == 'SELL' else "Recovery SELL"
                            logger.info(f"{tag} filled: "
                                       f"L{order_data['level']}.{child['idx']}")
                            # Per-ladder recycle: when this fill closed the
                            # whole ladder, immediately re-arm it at the same
                            # prices so the level keeps cycling without the
                            # global auto-restart re-anchoring everything.
                            if ladder_just_closed and self._recycle_in_place_enabled(
                                    order_data.get('strategy')):
                                self._recycle_ladder_in_place(
                                    order_data['strategy'], parent
                                )
                            if otype == 'SELL_RECOVERY':
                                # Drop from per-strategy recovery lot too.
                                self._drop_from_recovery_lot(
                                    order_data['strategy'],
                                    child.get('parent_level'),
                                    child.get('idx'),
                                )
                        else:
                            order_data['ladder']['status'] = 'closed'
                            logger.info(f"Sell order filled: Level {order_data['level']}")

                    # Remove from active orders
                    del self.active_orders[order_id]

            except Exception as e:
                logger.error(f"Error checking order {order_id}: {e}")

        return filled_orders

    def reconcile_with_exchange(self, strategies=None):
        """Verify saved active_orders against Binance after a restart.

        For each saved order, query the exchange:
          - FILLED: keep in active_orders so the next check_filled_orders()
            run picks it up (which records the position and queues a SELL).
          - CANCELED / EXPIRED / REJECTED: drop. If it was a child BUY, push
            the child back onto the pending queue so it can be retried.
          - NEW / PARTIALLY_FILLED: still live, keep.
          - Anything else (or query error): keep so we don't lose it; the
            regular loop will pick it up.

        If `strategies` is supplied, also scan all open orders on the exchange
        for each strategy symbol and adopt any orphan orders that exist on
        Binance but are missing from our state file. This protects against the
        crash window between create_limit_order() succeeding on Binance and
        the bot recording the response in active_orders (e.g. a hard power
        loss between the API call and the next state save).

        Returns a tuple (kept, missed_fills, dropped).
        """
        kept = 0
        missed_fills = 0
        dropped = 0
        for order_id, od in list(self.active_orders.items()):
            symbol = od["order"].get("symbol")
            try:
                status = self.client.get_order(symbol=symbol, order_id=order_id)
            except Exception as e:
                logger.warning(f"Reconcile: cannot query order {order_id} on {symbol}: {e}; "
                               f"keeping in active list")
                kept += 1
                continue

            s = status.get("status")
            if s == "FILLED":
                missed_fills += 1
                logger.info(f"Reconcile: order {order_id} ({od['type']} L{od['level']}) "
                            f"filled while offline - will be processed on next check")
                kept += 1
            elif s in ("CANCELED", "EXPIRED", "REJECTED", "PENDING_CANCEL"):
                dropped += 1
                logger.info(f"Reconcile: order {order_id} ({od['type']} L{od['level']}) "
                            f"is {s} on exchange; dropping")
                child = od.get("child")
                if child is not None and od["type"] == "BUY":
                    # Put the child back on the pending queue so it can be retried
                    queue = self._pending_children.setdefault(od["strategy"], [])
                    child["status"] = "pending"
                    queue.append(child)
                    queue.sort(key=lambda c: c["buy_price"], reverse=True)
                del self.active_orders[order_id]
            else:
                kept += 1

        logger.info(f"Reconcile complete: {kept} kept, {missed_fills} missed fills, "
                    f"{dropped} dropped")

        if strategies:
            adopted, unmatched = self._reconcile_orphan_orders(strategies)
            if adopted or unmatched:
                logger.info(f"Reconcile orphans: {adopted} adopted, {unmatched} unmatched warnings")

        return kept, missed_fills, dropped

    def _reconcile_orphan_orders(self, strategies):
        """Fetch open orders on Binance for each strategy symbol; for any that
        are not already in active_orders, try to match them back to a pending
        child or planned ladder. Adopt matches; warn loudly about unmatched
        orders so the user can manually cancel duplicates.

        Returns (adopted_count, unmatched_count).
        """
        adopted = 0
        unmatched = 0
        known_ids = {str(oid) for oid in self.active_orders.keys()}

        # Prime distribution queues so orphan child BUYs can be matched. This
        # is a no-op for already-populated queues (resumed from saved state).
        for strat in strategies:
            try:
                self.prime_distribution_queue(strat)
            except Exception as e:
                logger.warning(f"Reconcile orphans: cannot prime distribution queue "
                               f"for {strat.config['name']}: {e}")

        # Group strategies by symbol so we only query each symbol once.
        symbol_to_strategies = {}
        for strat in strategies:
            symbol = strat.config['pair']
            symbol_to_strategies.setdefault(symbol, []).append(strat)

        for symbol, strats in symbol_to_strategies.items():
            try:
                exchange_orders = self.client.get_open_orders(symbol=symbol)
            except Exception as e:
                logger.warning(f"Reconcile orphans: cannot fetch open orders for {symbol}: {e}")
                continue

            for ex_order in exchange_orders:
                ex_id = str(ex_order.get('orderId'))
                if ex_id in known_ids:
                    continue

                if self._try_adopt_orphan(ex_order, strats):
                    adopted += 1
                    known_ids.add(ex_id)
                else:
                    unmatched += 1
                    logger.warning(
                        f"Orphan order on Binance for {symbol}: id={ex_id} "
                        f"{ex_order.get('side')} qty={ex_order.get('origQty')} "
                        f"@ ${float(ex_order.get('price', 0)):.4f}. "
                        f"Not in saved state and could not be matched to a planned level. "
                        f"Manually cancel on Binance if this is a stale duplicate."
                    )

        return adopted, unmatched

    def _try_adopt_orphan(self, ex_order, strategies):
        """Match an orphan exchange order to a pending child / planned ladder
        by side + price (within a small tolerance). On match, register it in
        active_orders so the loop will pick it up. Returns True if adopted."""
        side = ex_order.get('side')
        ex_price = float(ex_order.get('price', 0) or 0)
        if ex_price <= 0:
            return False
        # 0.5% tolerance to absorb rounding when the bot rounds prices to the
        # exchange tick size before placing the order.
        price_tol = max(ex_price * 0.005, 1e-8)

        for strategy in strategies:
            # 1) Match against pending children (distribution mode BUY orders)
            if side == 'BUY':
                queue = self._pending_children.get(strategy.config['name'], [])
                for child in list(queue):
                    if abs(child['buy_price'] - ex_price) <= price_tol:
                        parent_ladder = child['parent_ladder']
                        order_id = ex_order.get('orderId')
                        self.active_orders[order_id] = {
                            'strategy': strategy.config['name'],
                            'level': child['parent_level'],
                            'type': 'BUY',
                            'order': ex_order,
                            'ladder': parent_ladder,
                            'child': child,
                            'placed_at': self._extract_order_time(ex_order),
                        }
                        child['status'] = 'placed'
                        parent_ladder['children_placed'] = parent_ladder.get('children_placed', 0) + 1
                        queue.remove(child)
                        logger.info(
                            f"Adopted orphan child BUY: {strategy.config['name']} "
                            f"L{child['parent_level']}.{child['idx']} @ ${ex_price:.4f} "
                            f"(orderId={order_id})"
                        )
                        return True

            # 2) Match against ladder-level buy/sell prices (sequential or plain mode,
            #    or a SELL placed for a filled child that we missed recording)
            for ladder in strategy.ladders:
                target_price = ladder.get('buy_price' if side == 'BUY' else 'sell_price')
                if not target_price or target_price <= 0:
                    continue
                if abs(target_price - ex_price) <= price_tol:
                    order_id = ex_order.get('orderId')
                    self.active_orders[order_id] = {
                        'strategy': strategy.config['name'],
                        'level': ladder['level'],
                        'type': side,
                        'order': ex_order,
                        'ladder': ladder,
                        'placed_at': self._extract_order_time(ex_order),
                    }
                    logger.info(
                        f"Adopted orphan {side}: {strategy.config['name']} "
                        f"L{ladder['level']} @ ${ex_price:.4f} (orderId={order_id})"
                    )
                    return True

        return False

    def cancel_all_orders(self, strategy_name=None):
        """Cancel all active orders (optionally for specific strategy)"""
        cancelled = []

        for order_id, order_data in list(self.active_orders.items()):
            if strategy_name is None or order_data['strategy'] == strategy_name:
                try:
                    self.client.cancel_order(
                        symbol=order_data['order']['symbol'],
                        order_id=order_id
                    )
                    cancelled.append(order_id)
                    del self.active_orders[order_id]
                except Exception as e:
                    logger.error(f"Failed to cancel order {order_id}: {e}")

        logger.info(f"Cancelled {len(cancelled)} orders")
        return cancelled
