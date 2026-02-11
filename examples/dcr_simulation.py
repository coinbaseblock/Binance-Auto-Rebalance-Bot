#!/usr/bin/env python3
"""
DCR Ladder Strategy Simulation
แสดงการจำลอง DCR Balanced Strategy พร้อม Martingale ladder
"""

import json
from pathlib import Path

# ANSI Colors for terminal
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def load_config():
    """Load DCR strategy config"""
    config_path = Path(__file__).parent.parent / "config" / "strategies" / "dcr_balanced.json"
    with open(config_path) as f:
        return json.load(f)

def simulate_dcr():
    """Run DCR simulation"""
    config = load_config()

    print(f"\n{Colors.BOLD}{'='*70}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}   🪙 DCR LADDER STRATEGY SIMULATION{Colors.ENDC}")
    print(f"{Colors.BOLD}{'='*70}{Colors.ENDC}\n")

    # Wallet Info
    wallet = config['wallet']
    initial_dcr = wallet['initial_dcr']
    initial_usdt = wallet['initial_usdt']
    entry_price = wallet['entry_price']

    portfolio_value = (initial_dcr * entry_price) + initial_usdt

    print(f"{Colors.CYAN}📊 WALLET STATUS{Colors.ENDC}")
    print(f"{'─'*50}")
    print(f"   DCR Holdings:     {initial_dcr:>10.2f} DCR")
    print(f"   Entry Price:      ${entry_price:>9.2f}")
    print(f"   DCR Value:        ${initial_dcr * entry_price:>9.2f}")
    print(f"   USDT Balance:     ${initial_usdt:>9.2f}")
    print(f"   {Colors.GREEN}Total Portfolio:   ${portfolio_value:>9.2f}{Colors.ENDC}")
    print()

    # Ladder Configuration
    ladder_config = config['ladder_config']
    base_gap = ladder_config['base_gap']
    fibonacci = ladder_config['fibonacci']
    num_ladders = ladder_config['ladders']
    unit_size = ladder_config['unit_size_dcr']

    print(f"{Colors.CYAN}⚙️  LADDER CONFIGURATION{Colors.ENDC}")
    print(f"{'─'*50}")
    print(f"   Base Gap:         {base_gap*100:>10.1f}%")
    print(f"   Number of Ladders:     {num_ladders:>5}")
    print(f"   Unit Size:        {unit_size:>10.1f} DCR")
    print(f"   Fibonacci Seq:    {fibonacci}")
    print()

    # Calculate Ladders
    current_price = entry_price

    print(f"{Colors.CYAN}📉 BUY LADDERS (ซื้อเมื่อราคาลง){Colors.ENDC}")
    print(f"{'─'*70}")
    print(f"{'Level':^7}{'Gap':^8}{'Cum.Gap':^10}{'Buy Price':^12}{'DCR Amt':^10}{'USDT Cost':^12}{'Units':^7}")
    print(f"{'─'*70}")

    cumulative_gap = 0
    total_usdt_required = 0
    total_dcr_bought = 0
    ladders = []

    # Base USDT per unit at level -1 (for true Martingale)
    first_ladder_gap = base_gap * fibonacci[0]
    first_buy_price = current_price * (1 - first_ladder_gap)
    base_usdt_per_unit = first_buy_price * unit_size

    for i in range(num_ladders):
        fib = fibonacci[i]
        gap = base_gap * fib
        cumulative_gap += gap

        buy_price = current_price * (1 - cumulative_gap)
        martingale_units = 2 ** i  # 1, 2, 4, 8, 16, 32

        # True Martingale: USDT doubles each level
        usdt_cost = martingale_units * base_usdt_per_unit
        dcr_amount = usdt_cost / buy_price

        total_usdt_required += usdt_cost
        total_dcr_bought += dcr_amount

        ladder = {
            'level': -(i+1),
            'gap': gap,
            'cumulative_gap': cumulative_gap,
            'buy_price': buy_price,
            'dcr_amount': dcr_amount,
            'usdt_cost': usdt_cost,
            'units': martingale_units
        }
        ladders.append(ladder)

        color = Colors.GREEN if i < 3 else Colors.YELLOW if i < 5 else Colors.RED
        print(f"  {ladder['level']:>3}   {gap*100:>5.1f}%   {cumulative_gap*100:>6.1f}%   "
              f"${buy_price:>8.2f}   {dcr_amount:>7.2f}   "
              f"{color}${usdt_cost:>8.2f}{Colors.ENDC}   {martingale_units:>3}x")

    print(f"{'─'*70}")
    print(f"{'TOTAL':^7}{'':^8}{cumulative_gap*100:>6.1f}%   {'':^12}{total_dcr_bought:>7.2f}   "
          f"{Colors.BOLD}${total_usdt_required:>8.2f}{Colors.ENDC}")
    print()

    # Sell Ladders
    print(f"{Colors.CYAN}📈 SELL LADDERS (ขายเมื่อราคาขึ้น){Colors.ENDC}")
    print(f"{'─'*70}")
    print(f"{'Level':^7}{'Buy@':^12}{'Sell@':^12}{'Profit/DCR':^12}{'Est.Profit':^12}")
    print(f"{'─'*70}")

    total_potential_profit = 0
    fee_rate = 0.001  # 0.1% trading fee

    for i, ladder in enumerate(ladders):
        buy_price = ladder['buy_price']
        # Sell price is at the previous level (or entry price for level -1)
        if i == 0:
            sell_price = current_price
        else:
            sell_price = ladders[i-1]['buy_price']

        # Calculate profit per DCR after fees
        buy_cost_per_dcr = buy_price * (1 + fee_rate)
        sell_revenue_per_dcr = sell_price * (1 - fee_rate)
        profit_per_dcr = sell_revenue_per_dcr - buy_cost_per_dcr

        dcr_amount = ladder['dcr_amount']
        estimated_profit = profit_per_dcr * dcr_amount
        total_potential_profit += estimated_profit

        color = Colors.GREEN if estimated_profit > 0 else Colors.RED
        print(f"  {ladder['level']:>3}   ${buy_price:>8.2f}   ${sell_price:>8.2f}   "
              f"${profit_per_dcr:>8.4f}   {color}${estimated_profit:>8.2f}{Colors.ENDC}")

    print(f"{'─'*70}")
    print(f"{'':^7}{'':^12}{'':^12}{'TOTAL':^12}"
          f"{Colors.GREEN}{Colors.BOLD}${total_potential_profit:>8.2f}{Colors.ENDC}")
    print()

    # Risk Analysis
    print(f"{Colors.CYAN}⚠️  RISK ANALYSIS{Colors.ENDC}")
    print(f"{'─'*50}")

    usdt_available = initial_usdt
    capital_utilization = (total_usdt_required / usdt_available) * 100 if usdt_available > 0 else 0

    risk_config = config['risk_management']
    stop_loss = risk_config['stop_loss_percent']
    take_profit = risk_config['take_profit_percent']

    # Worst case: all ladders triggered
    worst_price = ladders[-1]['buy_price']
    worst_dcr_value = (initial_dcr + total_dcr_bought) * worst_price
    worst_usdt_remaining = initial_usdt - total_usdt_required
    worst_portfolio = worst_dcr_value + worst_usdt_remaining
    worst_loss_percent = ((worst_portfolio - portfolio_value) / portfolio_value) * 100

    print(f"   USDT Required (all ladders): ${total_usdt_required:>8.2f}")
    print(f"   USDT Available:              ${usdt_available:>8.2f}")

    if capital_utilization <= 100:
        print(f"   Capital Utilization:         {Colors.GREEN}{capital_utilization:>7.1f}%{Colors.ENDC}")
    else:
        print(f"   Capital Utilization:         {Colors.RED}{capital_utilization:>7.1f}% ⚠️ INSUFFICIENT!{Colors.ENDC}")

    print(f"   Stop Loss:                   {stop_loss*100:>7.1f}%")
    print(f"   Take Profit:                 {take_profit*100:>7.1f}%")
    print()
    print(f"   {Colors.YELLOW}Worst Case (all ladders hit):{Colors.ENDC}")
    print(f"      Price drops to:           ${worst_price:>8.2f}")
    print(f"      Total DCR held:           {initial_dcr + total_dcr_bought:>8.2f} DCR")
    print(f"      Portfolio value:          ${worst_portfolio:>8.2f}")
    print(f"      Unrealized loss:          {Colors.RED}{worst_loss_percent:>7.1f}%{Colors.ENDC}")
    print()

    # Scenario Simulation
    print(f"{Colors.CYAN}🎯 SCENARIO SIMULATION{Colors.ENDC}")
    print(f"{'─'*50}")

    scenarios = [
        ("ราคาลง 5% แล้วกลับ", -0.05),
        ("ราคาลง 10% แล้วกลับ", -0.10),
        ("ราคาลง 15% แล้วกลับ", -0.15),
        ("ราคาลง 20% แล้วกลับ (เกือบหมด)", -0.20),
    ]

    for scenario_name, drop_percent in scenarios:
        target_price = current_price * (1 + drop_percent)

        # Count how many ladders would be triggered
        ladders_triggered = 0
        usdt_spent = 0
        dcr_bought = 0

        for ladder in ladders:
            if target_price <= ladder['buy_price']:
                ladders_triggered += 1
                usdt_spent += ladder['usdt_cost']
                dcr_bought += ladder['dcr_amount']

        # If price recovers, calculate profit
        recovery_profit = 0
        for i in range(ladders_triggered):
            ladder = ladders[i]
            buy_price = ladder['buy_price']
            sell_price = current_price if i == 0 else ladders[i-1]['buy_price']

            buy_cost = ladder['usdt_cost'] * (1 + fee_rate)
            sell_revenue = ladder['dcr_amount'] * sell_price * (1 - fee_rate)
            recovery_profit += (sell_revenue - buy_cost)

        status = f"{Colors.GREEN}+${recovery_profit:.2f}{Colors.ENDC}" if recovery_profit > 0 else f"{Colors.RED}${recovery_profit:.2f}{Colors.ENDC}"

        print(f"   {scenario_name}")
        print(f"      Price: ${target_price:.2f} → Ladders: {ladders_triggered} → Profit on recovery: {status}")
        print()

    print(f"{Colors.BOLD}{'='*70}{Colors.ENDC}")
    print(f"{Colors.GREEN}✅ Simulation Complete!{Colors.ENDC}")
    print(f"{Colors.BOLD}{'='*70}{Colors.ENDC}\n")

if __name__ == "__main__":
    simulate_dcr()
