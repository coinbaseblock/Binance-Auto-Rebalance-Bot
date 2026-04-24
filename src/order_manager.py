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
                       f"sell @ ${top['sell_price']:.4f} | "
                       f"total ${sum(c['usdt_cost'] for c in children):.2f}")

    def place_distribution_orders(self, strategy, current_price):
        """Entry point for distribution mode: build pending queue (if empty)
        and promote children whose price is near the market and that fit within
        the per-symbol open-order cap.

        Returns list of orders placed this call (may be empty when nothing is
        yet in proximity).
        """
        strategy_name = strategy.config['name']

        # Build the pending queue on first call (or after reset)
        if strategy_name not in self._pending_children:
            queue = []
            all_children = strategy.calculate_all_child_orders()
            for ladder in strategy.get_pending_ladders():
                ladder_children = all_children.get(ladder['level'], [])
                # Store a reference back to the parent ladder on each child so
                # we can update ladder state when children close.
                ladder['children_total'] = len(ladder_children)
                ladder['children_closed'] = 0
                ladder['children_placed'] = 0
                for child in ladder_children:
                    child['parent_ladder'] = ladder
                queue.extend(ladder_children)
            # Sort by buy_price descending so the top (closest to market) promotes first
            queue.sort(key=lambda c: c['buy_price'], reverse=True)
            self._pending_children[strategy_name] = queue
            logger.info(f"[{strategy_name}] Distribution queue initialized with {len(queue)} children")

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

    def _place_child_sell(self, strategy, child, parent_ladder, filled_qty):
        """Place child SELL at parent ladder's sell_price (hybrid pairing)."""
        strategy_name = strategy.config['name']
        symbol = strategy.config['pair']
        try:
            order = self.client.create_limit_order(
                symbol=symbol,
                side='SELL',
                quantity=filled_qty,
                price=parent_ladder['sell_price']
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
                       f"@ ${parent_ladder['sell_price']:.4f}")
            return order
        except Exception as e:
            logger.error(f"[{strategy_name}] Failed to place child SELL "
                        f"L{child['parent_level']}.{child['idx']}: {e}")
            return None

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

                    child = order_data.get('child')
                    # Distribution children use a composite level for portfolio
                    # bookkeeping so multiple children per ladder don't collide.
                    position_level = (
                        (order_data['level'], child['idx']) if child
                        else order_data['level']
                    )

                    if order_data['type'] == 'BUY':
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

                    elif order_data['type'] == 'SELL':
                        self.portfolio.close_position(
                            strategy_name=order_data['strategy'],
                            ladder_level=position_level,
                            sell_price=float(status['price']),
                            quantity=float(status['executedQty'])
                        )

                        if child is not None:
                            child['status'] = 'closed'
                            parent = order_data['ladder']
                            parent['children_closed'] = parent.get('children_closed', 0) + 1
                            # Mark the whole ladder closed only when every child
                            # has completed its cycle.
                            if parent['children_closed'] >= parent.get('children_total', 0) \
                                    and parent.get('children_total', 0) > 0:
                                parent['status'] = 'closed'
                            logger.info(f"Child SELL filled: "
                                       f"L{order_data['level']}.{child['idx']}")
                        else:
                            order_data['ladder']['status'] = 'closed'
                            logger.info(f"Sell order filled: Level {order_data['level']}")

                    # Remove from active orders
                    del self.active_orders[order_id]

            except Exception as e:
                logger.error(f"Error checking order {order_id}: {e}")

        return filled_orders

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
