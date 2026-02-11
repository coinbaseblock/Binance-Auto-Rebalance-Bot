# ตัวอย่างการใช้งาน Binance Auto Rebalance Bot

## สารบัญ
1. [Bot นี้ทำอะไร?](#bot-นี้ทำอะไร)
2. [หลักการทำงาน](#หลักการทำงาน)
3. [ตัวอย่างสถานการณ์จริง](#ตัวอย่างสถานการณ์จริง)
4. [การติดตั้งและเริ่มต้นใช้งาน](#การติดตั้งและเริ่มต้นใช้งาน)
5. [คำสั่งใช้งาน](#คำสั่งใช้งาน)
6. [การตั้งค่า Strategy](#การตั้งค่า-strategy)

---

## Bot นี้ทำอะไร?

Bot นี้ใช้กลยุทธ์ **Fibonacci-Martingale Grid Trading** เพื่อทำกำไรจากความผันผวนของราคา:

- **วาง Order ซื้อหลายระดับ** (Ladder) ใต้ราคาปัจจุบัน
- **ซื้ออัตโนมัติ** เมื่อราคาลงมาถึงระดับที่ตั้งไว้
- **ขายอัตโนมัติ** เมื่อราคากลับขึ้นมาถึงเป้าหมาย
- **เพิ่มขนาด Position** ตามหลัก Martingale (เพิ่มเท่าตัว)

---

## หลักการทำงาน

### 1. ระบบ Ladder (บันได)

สมมติ BTC ราคา **$100,000**:

```
ราคาปัจจุบัน: $100,000
        │
        ├── Ladder -1: ซื้อที่ $99,400 (ลง 0.6%)  → ขายที่ $100,600
        ├── Ladder -2: ซื้อที่ $98,800 (ลง 1.2%)  → ขายที่ $101,200
        ├── Ladder -3: ซื้อที่ $97,600 (ลง 2.4%)  → ขายที่ $102,400
        ├── Ladder -4: ซื้อที่ $95,800 (ลง 4.2%)  → ขายที่ $104,200
        ├── Ladder -5: ซื้อที่ $92,800 (ลง 7.2%)  → ขายที่ $107,200
        └── Ladder -6: ซื้อที่ $88,000 (ลง 12%)   → ขายที่ $112,000
```

### 2. ระบบ Martingale (เพิ่มเท่าตัว)

| Ladder | ขนาด BTC | มูลค่า USDT |
|--------|----------|-------------|
| -1 | 0.01 BTC | $994 |
| -2 | 0.02 BTC | $1,976 |
| -3 | 0.04 BTC | $3,904 |
| -4 | 0.08 BTC | $7,664 |
| -5 | 0.16 BTC | $14,848 |
| -6 | 0.32 BTC | $28,160 |

**รวม: 0.63 BTC | ~$57,546 USDT**

---

## ตัวอย่างสถานการณ์จริง

### สถานการณ์ที่ 1: ราคาลงเล็กน้อยแล้วกลับ

```
เวลา    ราคา BTC    เหตุการณ์                      การดำเนินการ
─────────────────────────────────────────────────────────────────────
08:00   $100,000    เริ่มต้น                       วาง BUY orders 6 ระดับ
09:00   $99,400     ราคาลงถึง Ladder -1           ✅ ซื้อ 0.01 BTC @ $99,400
                                                   → วาง SELL @ $100,600
10:00   $99,200     ราคาลงต่อ                      รอ...
11:00   $99,800     ราคากลับขึ้น                   รอ...
12:00   $100,600    ราคาถึงเป้า SELL              ✅ ขาย 0.01 BTC @ $100,600

💰 กำไร: $12 (จาก $994 → $1,006)
```

### สถานการณ์ที่ 2: ราคาลงลึกแล้วกลับ

```
เวลา    ราคา BTC    เหตุการณ์                      การดำเนินการ
─────────────────────────────────────────────────────────────────────
08:00   $100,000    เริ่มต้น                       วาง BUY orders 6 ระดับ
09:00   $99,400     Ladder -1 triggered           ✅ ซื้อ 0.01 BTC @ $99,400
10:00   $98,800     Ladder -2 triggered           ✅ ซื้อ 0.02 BTC @ $98,800
11:00   $97,600     Ladder -3 triggered           ✅ ซื้อ 0.04 BTC @ $97,600
12:00   $95,800     Ladder -4 triggered           ✅ ซื้อ 0.08 BTC @ $95,800

📊 ถือ: 0.15 BTC | ต้นทุน: $14,538 | ราคาเฉลี่ย: $96,920

─────────────────── ราคากลับขึ้น ───────────────────

14:00   $97,600     ราคาขึ้นมา                     รอ...
15:00   $98,800     Ladder -3 SELL filled         ✅ ขาย 0.04 BTC @ $98,800
                                                   กำไร: +$48
16:00   $99,400     Ladder -2 SELL filled         ✅ ขาย 0.02 BTC @ $99,400
                                                   กำไร: +$12
17:00   $100,600    Ladder -1 SELL filled         ✅ ขาย 0.01 BTC @ $100,600
                                                   กำไร: +$12
18:00   $104,200    Ladder -4 SELL filled         ✅ ขาย 0.08 BTC @ $104,200
                                                   กำไร: +$672

💰 กำไรรวม: $744
```

### สถานการณ์ที่ 3: Full Cycle (ลงหนักมาก)

```
BTC ลงจาก $100,000 → $88,000 (Ladder -6)

📊 Position ทั้งหมด:
┌─────────┬──────────┬────────────┬────────────┬──────────┐
│ Ladder  │ ซื้อที่   │ จำนวน BTC  │ ต้นทุน USDT │ ขายที่    │
├─────────┼──────────┼────────────┼────────────┼──────────┤
│ -1      │ $99,400  │ 0.01       │ $994       │ $100,600 │
│ -2      │ $98,800  │ 0.02       │ $1,976     │ $101,200 │
│ -3      │ $97,600  │ 0.04       │ $3,904     │ $102,400 │
│ -4      │ $95,800  │ 0.08       │ $7,664     │ $104,200 │
│ -5      │ $92,800  │ 0.16       │ $14,848    │ $107,200 │
│ -6      │ $88,000  │ 0.32       │ $28,160    │ $112,000 │
├─────────┼──────────┼────────────┼────────────┼──────────┤
│ รวม     │          │ 0.63 BTC   │ $57,546    │          │
└─────────┴──────────┴────────────┴────────────┴──────────┘

เมื่อราคากลับขึ้น ขายทุก Ladder:
💰 กำไรรวม: $3,870 (จาก $57,546 → $61,416)
📈 ROI: 6.7% ต่อรอบ
```

---

## การติดตั้งและเริ่มต้นใช้งาน

### ขั้นตอนที่ 1: Clone และติดตั้ง

```bash
# Clone repository
git clone https://github.com/yourusername/binance-auto-rebalance.git
cd binance-auto-rebalance

# สร้าง virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# ติดตั้ง dependencies
pip install -r requirements.txt
```

### ขั้นตอนที่ 2: ตั้งค่า API Keys

```bash
# คัดลอกไฟล์ตัวอย่าง
cp .env.example .env

# แก้ไขไฟล์ .env
nano .env  # หรือ editor ที่ต้องการ
```

ใส่ค่าต่อไปนี้ใน `.env`:

```bash
# API Keys จาก Binance
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# เริ่มต้นด้วย testnet ก่อนเสมอ!
BINANCE_TESTNET=true

# ทุนเริ่มต้น (USDT)
TOTAL_CAPITAL_USDT=10000

# Risk Management
STOP_LOSS_PERCENT=25
MAX_ALLOCATION_PER_COIN=0.20
```

### ขั้นตอนที่ 3: ทดสอบก่อนเสมอ!

```bash
# 1. Backtest ก่อน (ทดสอบกับข้อมูลย้อนหลัง - ไม่มีความเสี่ยง)
python main.py --mode backtest --strategies btc_conservative --start 2024-01-01 --end 2024-12-31

# 2. Paper Trading (ข้อมูลจริง แต่เงินจำลอง - ไม่มีความเสี่ยง)
python main.py --mode paper --strategies btc_conservative

# 3. Live Trading (ใช้เงินจริง - มีความเสี่ยง!)
python main.py --mode live --strategies btc_conservative
```

---

## คำสั่งใช้งาน

### Backtest (ทดสอบกับข้อมูลย้อนหลัง)

```bash
# ทดสอบ strategy เดียว
python main.py --mode backtest \
  --strategies btc_conservative \
  --start 2024-01-01 \
  --end 2024-12-31

# ทดสอบหลาย strategies
python main.py --mode backtest \
  --strategies btc_conservative eth_balanced bnb_aggressive \
  --start 2024-01-01 \
  --end 2024-12-31

# ทดสอบทุก strategies ที่เปิดใช้งาน
python main.py --mode backtest \
  --strategies all \
  --start 2024-01-01 \
  --end 2024-12-31
```

### Paper Trading (จำลองการเทรด)

```bash
# เทรดจำลอง strategy เดียว
python main.py --mode paper --strategies btc_conservative

# เทรดจำลองหลาย strategies
python main.py --mode paper --strategies btc_conservative eth_balanced

# เทรดจำลองทุก strategies
python main.py --mode paper --strategies all
```

### Live Trading (เทรดจริง)

```bash
# ⚠️ ใช้เงินจริง - ระวัง!
python main.py --mode live --strategies btc_conservative

# หลาย strategies
python main.py --mode live --strategies btc_conservative eth_balanced
```

---

## การตั้งค่า Strategy

### ไฟล์ Config ตัวอย่าง

`config/strategies/btc_conservative.json`:

```json
{
  "enabled": true,
  "name": "BTC Conservative",
  "pair": "BTCUSDT",

  "ladder_config": {
    "base_gap": 0.01,           // 1% ระหว่าง Ladder
    "ladders": 6,               // จำนวน Ladder
    "fibonacci": [1,1,2,3,5,8], // Fibonacci multipliers
    "unit_size_btc": 0.01       // ขนาดต่อ unit
  },

  "risk_management": {
    "safety_multiplier": 1.5,
    "stop_loss_percent": -0.25,  // -25% stop loss
    "take_profit_percent": 0.20  // +20% take profit
  }
}
```

### เปรียบเทียบ Strategies

| Strategy | Base Gap | Ladders | Total Swing | ความเสี่ยง |
|----------|----------|---------|-------------|-----------|
| BTC Conservative | 0.8% | 6 | ~20% | ต่ำ |
| ETH Balanced | 0.75% | 8 | ~25% | ปานกลาง |
| BNB Aggressive | 0.6% | 10 | ~85% | สูง |

### สร้าง Strategy ใหม่

สร้างไฟล์ใหม่ใน `config/strategies/`:

```json
// config/strategies/my_strategy.json
{
  "enabled": true,
  "name": "My Custom Strategy",
  "pair": "ETHUSDT",

  "ladder_config": {
    "base_gap": 0.008,          // 0.8%
    "ladders": 8,
    "fibonacci": [1,1,2,3,5,8,13,21],
    "unit_size_btc": 0.05       // 0.05 ETH ต่อ unit
  },

  "risk_management": {
    "safety_multiplier": 1.2,
    "stop_loss_percent": -0.30,
    "take_profit_percent": 0.15
  }
}
```

แล้วรัน:

```bash
python main.py --mode backtest --strategies my_strategy --start 2024-01-01 --end 2024-12-31
```

---

## ผลลัพธ์ตัวอย่าง

### Backtest Report

```json
{
  "strategy": "BTC Conservative",
  "period": {
    "start": "2024-01-01",
    "end": "2024-12-31",
    "days": 365
  },
  "capital": {
    "initial": 10000.0,
    "final": 11245.67,
    "profit": 1245.67,
    "roi_percent": 12.46
  },
  "trades": {
    "total": 48,
    "winning": 46,
    "losing": 2,
    "win_rate": 95.83,
    "avg_profit": 25.95
  }
}
```

---

## คำเตือนความเสี่ยง

1. **เริ่มด้วย Backtest เสมอ** - ทดสอบ strategy ก่อนใช้เงินจริง
2. **ใช้ Paper Trading** - ทดลองกับเงินจำลองก่อน
3. **เริ่มด้วยทุนน้อย** - อย่าใช้เงินทั้งหมดในครั้งเดียว
4. **ตั้ง Stop Loss** - ป้องกันการขาดทุนหนัก
5. **ตรวจสอบเป็นประจำ** - Bot ไม่ได้ perfect 100%

**การเทรด Cryptocurrency มีความเสี่ยงสูง อย่าลงทุนเงินที่ไม่พร้อมจะเสีย**
