#!/usr/bin/env python3
"""
Quick Start Example - Binance Auto Rebalance Bot

ตัวอย่างการใช้งาน Bot เบื้องต้น
- แสดงการคำนวณ Ladder prices
- แสดงการคำนวณ Position sizes (Martingale)
- แสดงผลกำไรที่คาดหวัง
"""

from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy import Strategy
from src.martingale import MartingaleCalculator


def print_header(title: str):
    """Print formatted header"""
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


def example_ladder_calculation():
    """ตัวอย่างการคำนวณ Ladder prices"""
    print_header("ตัวอย่างการคำนวณ Ladder Prices")

    # ราคาปัจจุบันของ BTC
    current_price = 100_000  # $100,000

    # ค่า config
    base_gap = 0.008  # 0.8%
    fibonacci = [1, 1, 2, 3, 5, 8]
    ladders = 6

    print(f"\nราคาปัจจุบัน: ${current_price:,.0f}")
    print(f"Base Gap: {base_gap*100}%")
    print(f"จำนวน Ladders: {ladders}")

    print(f"\n{'─'*60}")
    print(f"{'Ladder':<10} {'Fib':<6} {'Gap %':<10} {'Cumulative':<12} {'Buy Price':<12}")
    print(f"{'─'*60}")

    cumulative_gap = 0
    buy_prices = []

    for i in range(ladders):
        fib = fibonacci[i]
        gap = base_gap * fib
        cumulative_gap += gap
        buy_price = current_price * (1 - cumulative_gap)
        buy_prices.append(buy_price)

        print(f"{-i-1:<10} {fib:<6} {gap*100:.2f}%{'':<5} {cumulative_gap*100:.2f}%{'':<6} ${buy_price:,.0f}")

    print(f"{'─'*60}")
    print(f"\nTotal Swing: {cumulative_gap*100:.2f}%")
    print(f"Lowest Buy Price: ${buy_prices[-1]:,.0f}")

    return buy_prices


def example_martingale_sizing():
    """ตัวอย่างการคำนวณ Position Sizes ด้วย Martingale"""
    print_header("ตัวอย่างการคำนวณ Martingale Position Sizing")

    unit_size = 0.01  # BTC
    ladders = 6
    current_price = 100_000

    print(f"\nUnit Size: {unit_size} BTC")
    print(f"จำนวน Ladders: {ladders}")

    print(f"\n{'─'*60}")
    print(f"{'Ladder':<10} {'Units':<8} {'BTC':<10} {'USDT Value':<12}")
    print(f"{'─'*60}")

    total_btc = 0
    total_usdt = 0

    for i in range(ladders):
        # Martingale: เพิ่มเท่าตัวในแต่ละ Ladder
        units = 2 ** i
        btc_amount = unit_size * units
        usdt_value = btc_amount * current_price

        total_btc += btc_amount
        total_usdt += usdt_value

        print(f"{-i-1:<10} {units:<8} {btc_amount:<10.2f} ${usdt_value:,.0f}")

    print(f"{'─'*60}")
    print(f"{'Total':<10} {'':<8} {total_btc:<10.2f} ${total_usdt:,.0f}")

    print(f"\n⚠️  ต้องมีทุนอย่างน้อย ${total_usdt:,.0f} USDT สำหรับ Strategy นี้")


def example_profit_calculation():
    """ตัวอย่างการคำนวณกำไร"""
    print_header("ตัวอย่างการคำนวณกำไร")

    # สมมติ BTC ลงจาก $100,000 → $88,000 แล้วกลับมา
    current_price = 100_000
    base_gap = 0.008
    fibonacci = [1, 1, 2, 3, 5, 8]
    unit_size = 0.01
    fee_rate = 0.001  # 0.1% Binance fee

    print(f"\nสถานการณ์: BTC ลงจาก ${current_price:,} แล้วกลับมา")
    print(f"Fee Rate: {fee_rate*100}%")

    print(f"\n{'─'*70}")
    print(f"{'Ladder':<8} {'Buy Price':<12} {'Sell Price':<12} {'BTC':<8} {'Gross':<10} {'Net Profit':<10}")
    print(f"{'─'*70}")

    cumulative_gap = 0
    total_gross = 0
    total_net = 0
    total_cost = 0

    for i in range(6):
        fib = fibonacci[i]
        gap = base_gap * fib
        cumulative_gap += gap

        buy_price = current_price * (1 - cumulative_gap)
        sell_price = current_price * (1 + cumulative_gap)

        units = 2 ** i
        btc_amount = unit_size * units

        cost = buy_price * btc_amount
        revenue = sell_price * btc_amount
        gross_profit = revenue - cost

        # หัก fee ทั้งซื้อและขาย
        buy_fee = cost * fee_rate
        sell_fee = revenue * fee_rate
        net_profit = gross_profit - buy_fee - sell_fee

        total_cost += cost
        total_gross += gross_profit
        total_net += net_profit

        print(f"{-i-1:<8} ${buy_price:>10,.0f} ${sell_price:>10,.0f} {btc_amount:<8.2f} ${gross_profit:>8,.0f} ${net_profit:>8,.0f}")

    print(f"{'─'*70}")
    print(f"\n📊 สรุป (ถ้าราคาลงมาถึง Ladder -6 แล้วกลับขึ้น):")
    print(f"   ต้นทุนรวม: ${total_cost:,.0f}")
    print(f"   กำไรก่อนหัก Fee: ${total_gross:,.0f}")
    print(f"   กำไรหลังหัก Fee: ${total_net:,.0f}")
    print(f"   ROI: {(total_net/total_cost)*100:.2f}%")


def example_scenario_simulation():
    """จำลองสถานการณ์การเทรด"""
    print_header("จำลองสถานการณ์การเทรด")

    print("""
📈 สถานการณ์: BTC ราคาผันผวนระหว่าง $95,000 - $105,000

Timeline:
─────────────────────────────────────────────────────────────────

Day 1: ราคาเริ่มต้น $100,000
  08:00  เริ่ม Bot → วาง BUY orders ที่ทุก Ladder

Day 2: ราคาลง $99,400
  10:00  Ladder -1 filled → ซื้อ 0.01 BTC @ $99,400
         → วาง SELL order @ $100,600

Day 3: ราคากลับขึ้น $100,600
  14:00  SELL filled → ขาย 0.01 BTC @ $100,600
         💰 กำไร: $12 (หัก fee แล้ว ~$10)
         → วาง BUY order ใหม่ @ $99,400

Day 5: ราคาลงลึก $97,600
  09:00  Ladder -1 filled → ซื้อ 0.01 BTC @ $99,400
  11:00  Ladder -2 filled → ซื้อ 0.02 BTC @ $98,800
  15:00  Ladder -3 filled → ซื้อ 0.04 BTC @ $97,600
         📊 ถือ: 0.07 BTC | ต้นทุน: $6,874

Day 7: ราคากลับ $100,600
  10:00  Ladder -3 SELL filled → กำไร $48
  12:00  Ladder -2 SELL filled → กำไร $24
  14:00  Ladder -1 SELL filled → กำไร $12
         💰 กำไรรวม: $84 (หัก fee แล้ว ~$70)

─────────────────────────────────────────────────────────────────

📊 สรุป 7 วัน:
   • Trades ทั้งหมด: 8 (4 ซื้อ, 4 ขาย)
   • กำไรสุทธิ: ~$80
   • Win Rate: 100%
   • ROI: 0.8% ใน 7 วัน = ~42% ต่อปี (compounded)
""")


def main():
    """Run all examples"""
    print("\n" + "="*60)
    print(" 🤖 Binance Auto Rebalance Bot - Quick Start Examples")
    print("="*60)

    # 1. คำนวณ Ladder prices
    example_ladder_calculation()

    # 2. คำนวณ Position sizes
    example_martingale_sizing()

    # 3. คำนวณกำไร
    example_profit_calculation()

    # 4. จำลองสถานการณ์
    example_scenario_simulation()

    print_header("วิธีการใช้งานจริง")
    print("""
1. Backtest ก่อน (ทดสอบกับข้อมูลย้อนหลัง):
   python main.py --mode backtest --strategies btc_conservative \\
     --start 2024-01-01 --end 2024-12-31

2. Paper Trading (ข้อมูลจริง เงินจำลอง):
   python main.py --mode paper --strategies btc_conservative

3. Live Trading (⚠️ ใช้เงินจริง):
   python main.py --mode live --strategies btc_conservative

📖 อ่านเอกสารเพิ่มเติม: docs/USAGE_EXAMPLES_TH.md
""")


if __name__ == "__main__":
    main()
