import requests
import pandas as pd
import time
import os
import csv  # V2.1 新增
import numpy as np
from datetime import datetime, timedelta
from sqlmodel import Session, select
from .database import engine
from .models import ScanResult, SystemLog, SystemStatus
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

load_dotenv()

class ScannerEngine:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"
        
        # --- 网络代理 ---
        raw_proxy = os.getenv("PROXY_URL", "")
        if "127.0.0.1" in raw_proxy:
            raw_proxy = raw_proxy.replace("127.0.0.1", "host.docker.internal")
        elif "localhost" in raw_proxy:
            raw_proxy = raw_proxy.replace("localhost", "host.docker.internal")
        self.proxies = {"http": raw_proxy, "https": raw_proxy} if raw_proxy else None
        
        self.tg_token = os.getenv("TG_BOT_TOKEN")
        self.tg_chat_id = os.getenv("TG_CHAT_ID")
        
        self.scan_round = 0
        
        # --- V2.0 核心配置 ---
        self.flash_threshold = 0.03
        
        # 黑名单
        self.blacklist = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", 
            "AVAXUSDT", "TRXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT", "LTCUSDT", 
            "UNIUSDT", "BCHUSDT", "ETCUSDT", "FILUSDT", "ATOMUSDT", "XLMUSDT", "NEARUSDT",
            "CFXUSDT"
        ]

        # 状态管理
        self.leaderboard = {}
        self.cached_sentiment = None
        self.last_sentiment_update = 0
        
        # V2.1 初始化日志文件头
        self.csv_file = "scan_signals.csv"
        self.init_csv()

    # --- V2.1 新增: CSV 日志功能 ---
    def init_csv(self):
        # 如果文件不存在，写入表头
        if not os.path.exists(self.csv_file):
            try:
                with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Time", "Symbol", "Price", "Rule", "Score", "Strategy_Type",
                        "Change_180s", "RSI_15m", "Volatility_24h", "Bollinger_Pos", "Raw_Msg"
                    ])
            except Exception as e:
                print(f"CSV Init Error: {e}")

    def record_signal_to_csv(self, res: ScanResult, indicators: dict):
        """将信号和当时的技术指标写入 CSV"""
        try:
            with open(self.csv_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    res.symbol,
                    res.price,
                    res.rule_name,
                    res.score,
                    res.tags,
                    indicators.get("change_180s", 0),
                    indicators.get("rsi", 0),
                    indicators.get("volatility", 0),
                    indicators.get("bollinger", ""),
                    res.rule_name
                ])
        except Exception as e:
            self.log(f"CSV Write Error: {e}", "ERROR")

    def log(self, message, level="INFO"):
        print(f"[{level}] {message}")
        try:
            with Session(engine) as session:
                session.add(SystemLog(level=level, message=message))
                session.commit()
        except Exception: pass

    # --- V2.0 智能选币逻辑 ---
    def get_active_symbols(self):
        try:
            resp = requests.get(f"{self.base_url}/fapi/v1/ticker/24hr", proxies=self.proxies, timeout=10).json()
            valid_symbols = []
            for item in resp:
                sym = item['symbol']
                if not sym.endswith('USDT'): continue
                if sym in self.blacklist: continue
                
                quote_vol = float(item['quoteVolume'])
                price_change = abs(float(item['priceChangePercent']))
                
                # 规则: 成交额 > 5000万 且 波动 > 8%
                if quote_vol > 50000000 and price_change > 8.0:
                    valid_symbols.append(sym)
            
            if len(valid_symbols) < 3:
                self.log("高波动币种稀缺，启用备用选币标准 (>5%)", "WARNING")
                valid_symbols = []
                for item in resp:
                    sym = item['symbol']
                    if not sym.endswith('USDT') or sym in self.blacklist: continue
                    if float(item['quoteVolume']) > 30000000 and abs(float(item['priceChangePercent'])) > 5.0:
                        valid_symbols.append(sym)
            return valid_symbols
        except Exception as e:
            self.log(f"选币失败: {e}", "ERROR")
            return []

    def get_klines(self, symbol, interval='15m', limit=50):
        try:
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            resp = requests.get(f"{self.base_url}/fapi/v1/klines", params=params, proxies=self.proxies, timeout=5)
            df = pd.DataFrame(resp.json(), columns=['op_t','o','h','l','c','v','cl_t','qav','nt','tb','tq','ig'])
            df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
            return df
        except: return None

    # --- 180秒急速异动检测 ---
    def check_180s_shock(self, symbol):
        df = self.get_klines(symbol, interval='1m', limit=5)
        if df is None or len(df) < 4: return None

        current_price = df.iloc[-1]['c']
        price_3m_ago = df.iloc[-4]['o']
        
        if price_3m_ago == 0: return None

        pct_change = (current_price - price_3m_ago) / price_3m_ago
        abs_change = abs(pct_change)

        if abs_change >= self.flash_threshold:
            direction = "飙升" if pct_change > 0 else "闪崩"
            icon = "🚀" if pct_change > 0 else "📉"
            msg = f"{direction} {abs_change*100:.1f}% (180s)"
            score = 85 + int((abs_change - self.flash_threshold) * 100 * 2)
            
            res = ScanResult(
                symbol=symbol, price=current_price, change_percent=pct_change,
                vol_ratio=0, rule_name=msg, score=min(100, score),
                evo_state=icon, tags="⚡180s异动"
            )
            
            # V2.1 记录日志
            self.record_signal_to_csv(res, {
                "change_180s": round(pct_change * 100, 2),
                "strategy": "FlashShock"
            })
            return res
        return None

    # --- 综合分析逻辑 ---
    def analyze_single(self, symbol):
        # 1. 优先检测: 180秒
        flash_res = self.check_180s_shock(symbol)
        if flash_res: return flash_res

        # 2. 常规趋势检测
        df = self.get_klines(symbol, interval='15m', limit=50)
        if df is None or len(df) < 25: return None

        curr = df.iloc[-1]
        close = curr['c']
        
        # 指标计算
        df['sma'] = df['c'].rolling(window=20).mean()
        df['std'] = df['c'].rolling(window=20).std()
        upper_band = df.iloc[-1]['sma'] + (df.iloc[-1]['std'] * 2)
        
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        
        ma7 = df['c'].rolling(window=7).mean().iloc[-1]
        ma25 = df['c'].rolling(window=25).mean().iloc[-1]

        high_24 = df['h'].max()
        low_24 = df['l'].min()
        volatility = (high_24 - low_24) / low_24
        
        # 收集指标用于日志
        indicators = {
            "rsi": round(rsi, 2),
            "volatility": round(volatility * 100, 2),
            "bollinger": "Above" if close > upper_band else "Normal"
        }

        # --- 策略 A: 强力做空 ---
        if close > upper_band and rsi > 70 and volatility > 0.05:
            res = ScanResult(
                symbol=symbol, price=close, change_percent=0, vol_ratio=0,
                rule_name="做空:超买反转", score=90, evo_state="🐻", tags="高胜率"
            )
            self.record_signal_to_csv(res, indicators) # 记录 CSV
            return res

        # --- 策略 B: 顺势做多 ---
        if ma7 > ma25 and close > df.iloc[-1]['sma'] and volatility > 0.03:
            if rsi < 70:
                res = ScanResult(
                    symbol=symbol, price=close, change_percent=0, vol_ratio=0,
                    rule_name="做多:趋势增强", score=75, evo_state="🐂", tags="右侧"
                )
                self.record_signal_to_csv(res, indicators) # 记录 CSV
                return res

        return None

    def update_leaderboard(self, res: ScanResult):
        now_ts = time.time()
        sym = res.symbol
        if sym not in self.leaderboard:
            self.leaderboard[sym] = {
                "symbol": sym, "hits_today": 0, "hit_timestamps": [], 
                "last_trigger_time": "", "last_trigger_ts": 0,
                "reasons": set(), "max_vol_ratio": 0.0, "max_move": 0.0, "heat_score": 0
            }
        data = self.leaderboard[sym]
        data["hits_today"] += 1
        data["hit_timestamps"].append(now_ts)
        data["last_trigger_time"] = datetime.now().strftime("%H:%M:%S")
        data["last_trigger_ts"] = now_ts
        data["reasons"].add(res.rule_name)
        data["heat_score"] = res.score
        if abs(res.change_percent) > abs(data["max_move"]): data["max_move"] = res.change_percent

    def fetch_fear_and_greed(self):
        if time.time() - self.last_sentiment_update < 300 and self.cached_sentiment:
            return self.cached_sentiment
        try:
            url = "https://api.alternative.me/fng/?limit=2"
            r = requests.get(url, proxies=self.proxies, timeout=5)
            data = r.json()['data']
            today = data[0]; yesterday = data[1]
            score = int(today['value'])
            delta = score - int(yesterday['value'])
            if score <= 25: icon, color = "🥶", "text-blue-400"
            elif score <= 45: icon, color = "😨", "text-cyan-400"
            elif score <= 55: icon, color = "😐", "text-gray-400"
            elif score <= 75: icon, color = "🤑", "text-green-400"
            else: icon, color = "🚀", "text-red-500"
            result = {"score": score, "level": today['value_classification'], "icon": icon, "color_class": color, "delta": delta}
            self.cached_sentiment = result; self.last_sentiment_update = time.time()
            return result
        except:
            return {"score": 50, "level": "Unknown", "icon": "❓", "color_class": "text-gray-500", "delta": 0}

    def get_dashboard_data(self):
        now = time.time()
        clean_list = []
        stale_threshold = 3600 
        for sym, data in self.leaderboard.items():
            if now - data["last_trigger_ts"] > stale_threshold: continue
            hits_1h = len([t for t in data["hit_timestamps"] if now - t < 3600])
            item = data.copy()
            item["hits_1h"] = hits_1h
            item["reasons"] = list(data["reasons"])[:2]
            del item["hit_timestamps"]
            clean_list.append(item)
        clean_list.sort(key=lambda x: x["heat_score"], reverse=True)
        return {"market_heat": self.fetch_fear_and_greed(), "hot_list": clean_list[:20]}

    def send_telegram(self, res: ScanResult):
        if not self.tg_token or not self.tg_chat_id: return
        try:
            text = (f"🚨 <b>{res.symbol}</b>\nScore: {res.score}\nType: {res.evo_state} {res.tags}\nMsg: {res.rule_name}")
            requests.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage", json={"chat_id": self.tg_chat_id, "text": text, "parse_mode": "HTML"}, proxies=self.proxies, timeout=5)
        except: pass

    def run_scan(self):
        self.scan_round += 1
        self.log(f"开始 V2.1 Round {self.scan_round} 扫描...")
        symbols = self.get_active_symbols()
        if not symbols: 
            self.log("没有符合条件的币种 (成交量/波动率不足)", "WARNING")
            return
        
        try:
            with Session(engine) as session:
                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(self.analyze_single, sym): sym for sym in symbols}
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                            if result:
                                session.add(result)
                                session.commit()
                                self.update_leaderboard(result)
                                self.send_telegram(result)
                                self.log(f"命中: {result.symbol} {result.rule_name}")
                        except: pass
        except Exception as e: self.log(f"Scan error: {e}", "ERROR")
        
        try:
            with Session(engine) as session:
                st = session.get(SystemStatus, 1) or SystemStatus(id=1, last_heartbeat=datetime.now())
                st.last_heartbeat = datetime.now()
                st.scan_round = self.scan_round
                session.add(st)
                session.commit()
        except: pass
        self.log(f"Round {self.scan_round} 结束.")

scanner = ScannerEngine()