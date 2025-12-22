import requests
import pandas as pd
import time
import datetime
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from concurrent.futures import ThreadPoolExecutor

class Level1ScannerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("📡 异动雷达 (Level 1 Scanner) v0.0 - 网络修复版")
        self.root.geometry("1000x600")
        
        # --- 🌐 关键修改：网络代理设置 ---
        # 如果你仍然报错，请检查你的梯子软件设置里的 "HTTP 代理端口"
        # 常见的端口有：7890 (Clash), 10809 (v2rayN), 1087 (Mac)
        proxy_port = "7890"  # 👈 如果连不上，试着把这里改成 10809 或其他
        
        self.proxies = {
            "http": f"http://127.0.0.1:{proxy_port}",
            "https": f"http://127.0.0.1:{proxy_port}"
        }
        print(f"当前使用的代理配置: {self.proxies}")
        # --------------------------------

        self.base_url = "https://fapi.binance.com"
        self.symbols = []
        self.scan_interval = 120
        self.is_scanning = False
        
        # 异动阈值
        self.vol_factor = 2.5
        self.trend_threshold = 0.05
        self.accel_single = 0.08
        self.accel_accum = 0.07
        self.fail_shock = 0.06

        self.create_widgets()
        self.start_scan_thread()

    def create_widgets(self):
        # 1. 顶部状态栏
        self.status_frame = tk.Frame(self.root, bg="#f0f0f0", pady=10)
        self.status_frame.pack(fill="x")
        
        self.lbl_status = tk.Label(self.status_frame, text="系统初始化中...", font=("Arial", 12, "bold"), bg="#f0f0f0", fg="#333")
        self.lbl_status.pack(side="left", padx=20)
        
        self.lbl_time = tk.Label(self.status_frame, text="", font=("Arial", 10), bg="#f0f0f0", fg="#666")
        self.lbl_time.pack(side="right", padx=20)

        # 2. 数据表格区
        columns = ("time", "type", "symbol", "direction", "change", "vol", "note")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=20)
        
        self.tree.heading("time", text="时间")
        self.tree.heading("type", text="类型")
        self.tree.heading("symbol", text="币种")
        self.tree.heading("direction", text="方向")
        self.tree.heading("change", text="15m涨跌")
        self.tree.heading("vol", text="量比")
        self.tree.heading("note", text="异动说明 (结构/形态)")

        self.tree.column("time", width=80, anchor="center")
        self.tree.column("type", width=60, anchor="center")
        self.tree.column("symbol", width=100, anchor="center")
        self.tree.column("direction", width=80, anchor="center")
        self.tree.column("change", width=80, anchor="center")
        self.tree.column("vol", width=80, anchor="center")
        self.tree.column("note", width=300, anchor="w")

        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        self.tree.tag_configure('up', foreground='green')
        self.tree.tag_configure('down', foreground='red')
        self.tree.tag_configure('warn', foreground='#FF8C00')

    def update_status(self, text, color="black"):
        self.lbl_status.config(text=text, fg=color)
        # 确保在主线程更新UI
        self.root.update_idletasks()

    def update_clock(self):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.lbl_time.config(text=f"Last Check: {now}")

    def get_active_symbols(self):
        try:
            url = f"{self.base_url}/fapi/v1/exchangeInfo"
            # 🔥 修改点：加入了 proxies 参数
            resp = requests.get(url, timeout=15, proxies=self.proxies).json()
            self.symbols = [
                s['symbol'] for s in resp['symbols'] 
                if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL'
            ]
            return len(self.symbols)
        except Exception as e:
            print(f"连接错误详情: {e}")
            return 0

    def get_klines(self, symbol, interval='15m', limit=50):
        try:
            url = f"{self.base_url}/fapi/v1/klines"
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            # 🔥 修改点：加入了 proxies 参数
            resp = requests.get(url, params=params, timeout=10, proxies=self.proxies)
            data = resp.json()
            df = pd.DataFrame(data, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume', 
                'close_time', 'qav', 'num_trades', 'taker_base', 'taker_quote', 'ignore'
            ])
            cols = ['open', 'high', 'low', 'close', 'volume']
            df[cols] = df[cols].astype(float)
            return df
        except:
            return None

    def analyze_symbol(self, symbol):
        # 这里的逻辑保持不变，但 get_klines 内部已经修复了网络
        df_15m = self.get_klines(symbol, '15m', 50)
        if df_15m is None or len(df_15m) < 25: return None

        curr = df_15m.iloc[-1]
        
        close = curr['close']
        open_p = curr['open']
        high = curr['high']
        low = curr['low']
        vol = curr['volume']
        
        pct_change = (close - open_p) / open_p
        abs_change = abs(pct_change)
        
        vol_ma20 = df_15m['volume'].iloc[-21:-1].mean()
        if vol_ma20 == 0: vol_ma20 = 1
        vol_ratio = vol / vol_ma20

        high_4h = df_15m['high'].iloc[-17:-1].max()
        low_4h = df_15m['low'].iloc[-17:-1].min()

        alerts = []
        alert_time = datetime.datetime.now().strftime("%H:%M")

        # A类
        if abs_change >= self.trend_threshold and vol_ratio >= self.vol_factor:
            if pct_change > 0 and close > high_4h:
                alerts.append((alert_time, 'A', symbol, '📈 上涨', f"+{pct_change*100:.1f}%", f"x{vol_ratio:.1f}", f"突破4H高点 {high_4h}", 'up'))
            elif pct_change < 0 and close < low_4h:
                alerts.append((alert_time, 'A', symbol, '📉 下跌', f"{pct_change*100:.1f}%", f"x{vol_ratio:.1f}", f"跌破4H低点 {low_4h}", 'down'))

        # B类
        is_type_a = len(alerts) > 0
        if not is_type_a:
            if abs_change >= self.accel_single:
                 direction = '📈 上涨' if pct_change > 0 else '📉 下跌'
                 tag = 'up' if pct_change > 0 else 'down'
                 alerts.append((alert_time, 'B', symbol, direction, f"{pct_change*100:.1f}%", f"x{vol_ratio:.1f}", "单根K线极端情绪", tag))

        # C类
        shock_range = (high - low) / open_p
        if shock_range >= self.fail_shock and vol_ratio >= 2.0:
             if pct_change > 0 and close < high_4h:
                 upper_wick = (high - close) / open_p
                 if upper_wick > 0.02:
                     alerts.append((alert_time, 'C', symbol, '⚠️ 诱多?', f"+{pct_change*100:.1f}%", f"x{vol_ratio:.1f}", "放量冲高回落", 'warn'))
             elif pct_change < 0 and close > low_4h:
                 lower_wick = (close - low) / open_p
                 if lower_wick > 0.02:
                     alerts.append((alert_time, 'C', symbol, '⚠️ 诱空?', f"{pct_change*100:.1f}%", f"x{vol_ratio:.1f}", "放量探底回升", 'warn'))
        
        return alerts

    def start_scan_thread(self):
        thread = threading.Thread(target=self.scan_loop, daemon=True)
        thread.start()

    def scan_loop(self):
        self.update_status("正在连接 Binance (检查代理中)...", "blue")
        count = self.get_active_symbols()
        
        if count == 0:
            # 失败提示更具体
            self.update_status(f"连接失败! 请确认代理端口是否为 7890", "red")
            return
        
        self.update_status(f"监控中 - 标的数量: {count}", "green")

        while True:
            self.update_status(f"⚡ 正在扫描全市场 ({count} 个)...", "blue")
            
            results = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(self.analyze_symbol, sym): sym for sym in self.symbols}
                for future in futures:
                    res = future.result()
                    if res:
                        results.extend(res)
            
            if results:
                for item in results:
                    values = item[:-1]
                    tag = item[-1]
                    self.tree.insert("", 0, values=values, tags=(tag,))
            
            self.update_clock()
            self.update_status(f"💤 休眠中 (等待 {self.scan_interval}秒)...", "black")
            
            for i in range(self.scan_interval, 0, -1):
                time.sleep(1)
                if i % 10 == 0:
                     self.lbl_time.config(text=f"Next Scan: {i}s")

if __name__ == "__main__":
    root = tk.Tk()
    app = Level1ScannerGUI(root)
    root.mainloop()