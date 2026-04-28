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
                    'ladder': ladder
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
                'ladder': ladder
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

    def log_planned_distribution(self, strategy):
        """Log planned children per ladder for visibility."""
        if not self.is_distribution_mode(strategy):
            return
        all_children = strategy.calculate_all_child_orders()
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

        queue = []
        all_children = strategy.calculate_all_child_orders()
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

    def place_distribution_orders(self, strategy, current_price):
        """Entry point for distribution mode: build pending queue (if empty)
        and promote children whose price is near the market and that fit within
        the per-symbol open-order cap.

        Returns list of orders placed this call (may be empty when nothing is
        yet in proximity).
        """
        self.prime_distribution_queue(strategy)
        return self._promote_pending_children(strategy, current_price)

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

            order = self._place_child_buy(strategy, child)
            if order is not None:
                queue.remove(child)
                placed.append(order)
                symbol_open_orders += 1

        return placed

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
            status = self.client.client.get_order(symbol=symbol, orderId=order_id)
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
        logger.info(f"[{strategy_name}] Recovery: merged SELL filled @ ${sell_price:.4f}, "
                   f"{len(children)} children closed (qty {executed_qty:.6f})")
        # Recovery cycle complete — clear pool so the next drawdown starts fresh.
        self._recovery_lots.pop(strategy_name, None)

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
                status = self.client.client.get_order(
                    symbol=order_data['order']['symbol'],
                    orderId=order_id
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
                    else:
                        position_level = (
                            (order_data['level'], child['idx']) if child
                            else order_data['level']
                        )

                    if otype == 'BUY':
                        self.portfolio.add_position(
                            strategy_name=order_data['strategy'],
                            ladder_level=position_level,
                            buy_price=float(status['price']),
                            quantity=float(status['executedQty']),
                            cost=float(status['cummulativeQuoteQty'])
                        )
                        order_data['ladder']['status'] = 'active'

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
                            if parent.get('children_total', 0) > 0 \
                                    and parent['children_closed'] >= parent['children_total']:
                                parent['status'] = 'closed'
                            tag = "Child SELL" if otype == 'SELL' else "Recovery SELL"
                            logger.info(f"{tag} filled: "
                                       f"L{order_data['level']}.{child['idx']}")
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
                status = self.client.client.get_order(symbol=symbol, orderId=order_id)
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
