import requests
import pandas as pd
import time
import datetime
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3

# 禁用安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class Level1ScannerV05:
    def __init__(self, root):
        self.root = root
        self.root.title("📡 异动雷达 (Level 1 Scanner) V0.5 - 专业操盘手 UI 版")
        self.root.geometry("1400x900")
        
        # --- 🎨 UI 样式美化 ---
        self.setup_style()
        
        # --- 🌐 网络设置 ---
        self.proxy_port = "7890" 
        self.proxies = {
            "http": f"http://127.0.0.1:{self.proxy_port}",
            "https": f"http://127.0.0.1:{self.proxy_port}"
        }
        
        # --- 🧠 核心数据结构 ---
        self.evolution_memory = {} 
        self.watchlist = set() 
        self.new_listings = [] 
        self.top_movers_12h = [] 
        self.scan_round = 0 
        self.base_url = "https://fapi.binance.com"
        self.symbols_info = {} 
        self.scan_interval = 120
        
        # 启动 UI 和 线程
        self.setup_ui()
        self.start_scan_thread()

    def setup_style(self):
        # 配置全局样式
        style = ttk.Style()
        style.theme_use('clam') # 使用 clam 主题，比默认的现代
        
        # 表格样式
        style.configure("Treeview", 
                        background="white",
                        foreground="black",
                        rowheight=25,
                        fieldbackground="white",
                        font=("Arial", 10))
        style.map('Treeview', background=[('selected', '#E3F2FD')], foreground=[('selected', 'black')])
        
        # 表头样式
        style.configure("Treeview.Heading", 
                        font=("Arial", 10, "bold"),
                        background="#f0f0f0")
        
        # 进度条样式
        style.configure("Horizontal.TProgressbar", background="#4CAF50")

    def setup_ui(self):
        # ==========================================================
        # 1️⃣ 顶部：仪表盘 (Status & Progress) - 必须显眼
        # ==========================================================
        top_frame = tk.Frame(self.root, bg="#ECEFF1", pady=10, padx=15, relief="groove", bd=1)
        top_frame.pack(fill="x", side="top")

        # 左：状态与时间
        status_frame = tk.Frame(top_frame, bg="#ECEFF1")
        status_frame.pack(side="left")
        
        self.lbl_round = tk.Label(status_frame, text="Round 0", font=("Arial", 14, "bold"), bg="#ECEFF1", fg="#37474F")
        self.lbl_round.pack(anchor="w")
        self.lbl_update_time = tk.Label(status_frame, text="Last Update: --:--:--", font=("Consolas", 10), bg="#ECEFF1", fg="#78909C")
        self.lbl_update_time.pack(anchor="w")

        # 中：进度条
        progress_frame = tk.Frame(top_frame, bg="#ECEFF1", padx=40)
        progress_frame.pack(side="left", fill="x", expand=True)
        
        self.lbl_progress_info = tk.Label(progress_frame, text="System Standby", font=("Arial", 10, "bold"), bg="#ECEFF1", fg="#1976D2")
        self.lbl_progress_info.pack(anchor="w", pady=(0, 2))
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, length=400)
        self.progress_bar.pack(fill="x")

        # 右：控制区
        ctrl_frame = tk.Frame(top_frame, bg="#ECEFF1")
        ctrl_frame.pack(side="right")
        
        self.debug_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl_frame, text="🔧 验证模式", variable=self.debug_mode).pack(side="left", padx=10)
        ttk.Button(ctrl_frame, text="🗑️ 重置数据", command=self.clear_all).pack(side="left")

        # ==========================================================
        # 主分割：上部分(核心+背景) vs 下部分(历史+榜单)
        # ==========================================================
        main_paned = tk.PanedWindow(self.root, orient="vertical", sashrelief="raised", sashwidth=4, bg="#cfd8dc")
        main_paned.pack(fill="both", expand=True)

        # ==========================================================
        # 2️⃣ 中部区域：左雷达 + 右背景
        # ==========================================================
        mid_paned = tk.PanedWindow(main_paned, orient="horizontal", sashrelief="raised", sashwidth=4, bg="#cfd8dc")
        main_paned.add(mid_paned, height=500) # 给上半部分分配更多初始高度

        # --- 左侧：核心异动决策榜 ---
        frame_main = tk.LabelFrame(mid_paned, text="🦅 核心异动决策榜 (Decision Board)", font=("Arial", 11, "bold"), bg="white", fg="#D32F2F")
        mid_paned.add(frame_main, width=900) # 主区宽一点

        cols_signal = ("evo", "score", "time", "symbol", "price", "change", "vol", "tags", "reason")
        self.tree_signal = ttk.Treeview(frame_main, columns=cols_signal, show="headings", style="Treeview")
        
        # 配置列 (显性化数值)
        self.tree_signal.heading("evo", text="演化"); self.tree_signal.column("evo", width=50, anchor="center")
        self.tree_signal.heading("score", text="评分"); self.tree_signal.column("score", width=50, anchor="center")
        self.tree_signal.heading("time", text="时间"); self.tree_signal.column("time", width=70, anchor="center")
        self.tree_signal.heading("symbol", text="币种"); self.tree_signal.column("symbol", width=100, anchor="center")
        self.tree_signal.heading("price", text="价格 ($)"); self.tree_signal.column("price", width=90, anchor="e")
        self.tree_signal.heading("change", text="15m 涨跌"); self.tree_signal.column("change", width=80, anchor="center")
        self.tree_signal.heading("vol", text="量比"); self.tree_signal.column("vol", width=60, anchor="center")
        self.tree_signal.heading("tags", text="标签 (Context)"); self.tree_signal.column("tags", width=150, anchor="w")
        self.tree_signal.heading("reason", text="触发原因"); self.tree_signal.column("reason", width=200, anchor="w")
        
        scrollbar_main = ttk.Scrollbar(frame_main, orient="vertical", command=self.tree_signal.yview)
        self.tree_signal.configure(yscrollcommand=scrollbar_main.set)
        scrollbar_main.pack(side="right", fill="y")
        self.tree_signal.pack(fill="both", expand=True)
        
        # 颜色标签
        self.tree_signal.tag_configure('strong', background='#E8F5E9', foreground='#2E7D32') # 绿色背景
        self.tree_signal.tag_configure('watchlist', background='#FFF3E0', foreground='#E65100') # 橙色背景
        self.tree_signal.tag_configure('weak', foreground='#90A4AE') # 灰色文字

        # --- 右侧：轻量级市场背景 ---
        frame_context = tk.Frame(mid_paned, bg="white")
        mid_paned.add(frame_context, width=400)
        
        tk.Label(frame_context, text="🌊 实时波动 (Top Activity)", font=("Arial", 10, "bold"), bg="white", anchor="w").pack(fill="x", padx=5, pady=5)
        
        cols_mkt = ("sym", "chg", "vol")
        self.tree_market = ttk.Treeview(frame_context, columns=cols_mkt, show="headings")
        self.tree_market.heading("sym", text="币种"); self.tree_market.column("sym", width=100)
        self.tree_market.heading("chg", text="涨跌"); self.tree_market.column("chg", width=80, anchor="center")
        self.tree_market.heading("vol", text="量比"); self.tree_market.column("vol", width=60, anchor="center")
        self.tree_market.pack(fill="both", expand=True)

        # ==========================================================
        # 3️⃣ 底部区域：Tab 仓库 (历史 / 新币 / 12h)
        # ==========================================================
        self.notebook = ttk.Notebook(main_paned)
        main_paned.add(self.notebook)

        # Tab 1: 历史信号 (Module 4)
        tab_history = tk.Frame(self.notebook, bg="#f5f5f5")
        self.notebook.add(tab_history, text="📜 历史信号流 (History)")
        
        cols_hist = ("round", "time", "symbol", "price", "change", "vol", "score")
        self.tree_history = ttk.Treeview(tab_history, columns=cols_hist, show="headings")
        for c in cols_hist: 
            self.tree_history.heading(c, text=c.capitalize()); 
            self.tree_history.column(c, width=100, anchor="center")
        self.tree_history.pack(fill="both", expand=True)

        # Tab 2: 7天新币 (Module 4)
        tab_new = tk.Frame(self.notebook, bg="#f5f5f5")
        self.notebook.add(tab_new, text="🆕 最近7天新币 (New Listings)")
        
        cols_new = ("symbol", "price", "change12h", "days")
        self.tree_new = ttk.Treeview(tab_new, columns=cols_new, show="headings")
        self.tree_new.heading("symbol", text="币种"); self.tree_new.heading("price", text="价格")
        self.tree_new.heading("change12h", text="12h 趋势"); self.tree_new.heading("days", text="上线天数")
        self.tree_new.pack(fill="both", expand=True)
        self.tree_new.bind("<Double-1>", self.on_add_watchlist) # 双击关注

        # Tab 3: 12h 涨跌榜 (Module 4) - 左右分栏
        tab_12h = tk.Frame(self.notebook, bg="#f5f5f5")
        self.notebook.add(tab_12h, text="⏱ 12h 涨跌幅 Top10")
        
        # 12h 内部左右分栏
        frame_12h_up = tk.LabelFrame(tab_12h, text="🔥 12h 涨幅榜 (Gainers)", fg="green")
        frame_12h_up.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        
        frame_12h_down = tk.LabelFrame(tab_12h, text="❄️ 12h 跌幅榜 (Losers)", fg="red")
        frame_12h_down.pack(side="right", fill="both", expand=True, padx=5, pady=5)
        
        cols_12h = ("symbol", "change", "price")
        
        self.tree_12h_up = ttk.Treeview(frame_12h_up, columns=cols_12h, show="headings")
        self.tree_12h_up.heading("symbol", text="币种"); self.tree_12h_up.heading("change", text="12h 涨幅"); self.tree_12h_up.heading("price", text="价格")
        self.tree_12h_up.pack(fill="both", expand=True)
        self.tree_12h_up.bind("<Double-1>", self.on_add_watchlist)

        self.tree_12h_down = ttk.Treeview(frame_12h_down, columns=cols_12h, show="headings")
        self.tree_12h_down.heading("symbol", text="币种"); self.tree_12h_down.heading("change", text="12h 跌幅"); self.tree_12h_down.heading("price", text="价格")
        self.tree_12h_down.pack(fill="both", expand=True)
        self.tree_12h_down.bind("<Double-1>", self.on_add_watchlist)

    # --- 交互与逻辑 ---
    def clear_all(self):
        self.evolution_memory = {}
        for tree in [self.tree_signal, self.tree_history, self.tree_new, self.tree_12h_up, self.tree_12h_down, self.tree_market]:
            for item in tree.get_children(): tree.delete(item)
        self.watchlist.clear()
        messagebox.showinfo("系统", "数据已重置")

    def on_add_watchlist(self, event):
        tree = event.widget
        selected = tree.selection()
        if not selected: return
        item = tree.item(selected[0])
        symbol = item['values'][0]
        
        if symbol in self.watchlist:
            self.watchlist.remove(symbol)
            print(f"Removed: {symbol}")
        else:
            self.watchlist.add(symbol)
            print(f"Added: {symbol}")
            messagebox.showinfo("Watchlist", f"已加入监控: {symbol}")

    def get_active_symbols(self):
        try:
            resp = requests.get(f"{self.base_url}/fapi/v1/exchangeInfo", proxies=self.proxies, verify=False).json()
            symbols = []
            curr_time = time.time() * 1000
            for s in resp['symbols']:
                if s['status']=='TRADING' and s['quoteAsset']=='USDT' and s['contractType']=='PERPETUAL':
                    symbols.append(s['symbol'])
                    onboard = s.get('onboardDate', 0)
                    self.symbols_info[s['symbol']] = {'days': (curr_time - onboard)/(86400000)}
            return symbols
        except: return []

    def analyze_single(self, symbol, thresholds):
        df = self.get_klines(symbol)
        if df is None or len(df) < 25: return None, None, None

        curr = df.iloc[-1]
        close = curr['c']; open_p = curr['o']; vol = curr['v']
        
        pct_change = (close - open_p) / open_p
        abs_change = abs(pct_change)
        vol_ma20 = df['v'].iloc[-21:-1].mean()
        vol_ratio = vol / (vol_ma20 if vol_ma20 > 0 else 1)

        # 12h Change
        price_12h_ago = df.iloc[0]['c']
        change_12h = (close - price_12h_ago) / price_12h_ago

        # Lists Data
        info = self.symbols_info.get(symbol, {})
        new_data = None
        if info.get('days', 999) <= 7:
            new_data = {"symbol": symbol, "price": close, "change12h": change_12h, "days": info['days']}
        
        top_12h_data = {"symbol": symbol, "change": change_12h, "price": close}

        # Scanner Logic
        triggered = False
        reason = ""
        high_4h = df['h'].iloc[-17:-1].max(); low_4h = df['l'].iloc[-17:-1].min()
        
        if abs_change >= thresholds['trend'] and vol_ratio >= thresholds['vol']:
            triggered = True; reason = "突破4H" if pct_change > 0 else "跌破4H"
        if not triggered and abs_change >= thresholds['accel']: triggered = True; reason = "剧烈波动"
        
        is_watched = symbol in self.watchlist
        if is_watched and abs_change >= 0.01: triggered = True; reason = f"⭐关注异动 {reason}"

        alert = None
        if triggered:
            is_first = symbol not in self.evolution_memory
            score = 50 + int(abs_change * 1000) + int(vol_ratio * 5)
            if is_watched: score += 20
            
            evo = "⚖️"
            if symbol in self.evolution_memory:
                prev = self.evolution_memory[symbol][-1]
                if score > prev['score']: evo = "🚀"
                elif score < prev['score']: evo = "📉"
            
            tags = []
            if is_watched: tags.append("⭐")
            if info.get('days', 999) <= 7: tags.append("🆕")
            if abs(change_12h) > 0.15: tags.append("🚀12h强")
            
            alert = {
                "evo": evo, "score": min(99, score), "time": datetime.datetime.now().strftime("%H:%M"),
                "symbol": symbol, "price": close, "change": pct_change, "vol": vol_ratio,
                "tags": " ".join(tags), "reason": reason, "is_watched": is_watched, "round": self.scan_round
            }

        return alert, {"sym": symbol, "chg": pct_change, "vol": vol_ratio}, new_data, top_12h_data

    def start_scan_thread(self):
        t = threading.Thread(target=self.scan_loop, daemon=True)
        t.start()

    def scan_loop(self):
        self.lbl_progress_info.config(text="Connecting to Binance...")
        self.symbols = self.get_active_symbols()
        
        while True:
            self.scan_round += 1
            self.lbl_round.config(text=f"Round {self.scan_round}")
            
            # Reset containers
            self.new_listings = []; self.top_movers_12h = []
            alerts = []; markets = []
            
            thresholds = {"trend": 0.05, "vol": 2.5, "accel": 0.08}
            if self.debug_mode.get(): thresholds = {"trend": 0.02, "vol": 1.5, "accel": 0.03}
            
            completed = 0
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(self.analyze_single, sym, thresholds): sym for sym in self.symbols}
                for future in as_completed(futures):
                    completed += 1
                    self.progress_var.set((completed/len(self.symbols))*100)
                    # 进度条上方显示当前正在扫描的币
                    self.lbl_progress_info.config(text=f"Scanning: {futures[future]} [{int((completed/len(self.symbols))*100)}%]")
                    
                    try:
                        res = future.result()
                        if res[0]: alerts.append(res[0])
                        if res[1]: markets.append(res[1])
                        if res[2]: self.new_listings.append(res[2])
                        if res[3]: self.top_movers_12h.append(res[3])
                    except: pass
            
            self.update_ui(alerts, markets)
            
            # Record Evo
            for a in alerts:
                if a['symbol'] not in self.evolution_memory: self.evolution_memory[a['symbol']] = []
                self.evolution_memory[a['symbol']].append(a)

            self.lbl_progress_info.config(text="Scan Complete. Waiting...")
            self.lbl_update_time.config(text=f"Last Update: {datetime.datetime.now().strftime('%H:%M:%S')}")
            
            for i in range(self.scan_interval, 0, -1):
                time.sleep(1)

    def update_ui(self, alerts, markets):
        # 1. Main Signal
        for item in self.tree_signal.get_children(): self.tree_signal.delete(item)
        alerts.sort(key=lambda x: x['score'], reverse=True)
        for r in alerts:
            tag = 'watchlist' if r['is_watched'] else ('strong' if r['evo']=="🚀" else '')
            vals = (r['evo'], r['score'], r['time'], r['symbol'], f"{r['price']:.4f}", 
                    f"{r['change']*100:+.2f}%", f"x{r['vol']:.1f}", r['tags'], r['reason'])
            self.tree_signal.insert("", "end", values=vals, tags=(tag,))
            
            # Add to History (Bottom Tab 1)
            hist_vals = (f"#{r['round']}", r['time'], r['symbol'], f"{r['price']:.4f}", 
                         f"{r['change']*100:+.2f}%", f"x{r['vol']:.1f}", r['score'])
            self.tree_history.insert("", 0, values=hist_vals)

        # 2. Market Context (Right)
        markets.sort(key=lambda x: abs(x['chg']), reverse=True)
        for item in self.tree_market.get_children(): self.tree_market.delete(item)
        for m in markets[:20]:
            self.tree_market.insert("", "end", values=(m['sym'], f"{m['chg']*100:+.2f}%", f"x{m['vol']:.1f}"))

        # 3. New Listings (Bottom Tab 2)
        self.new_listings.sort(key=lambda x: x['days'])
        for item in self.tree_new.get_children(): self.tree_new.delete(item)
        for n in self.new_listings:
            self.tree_new.insert("", "end", values=(n['symbol'], f"{n['price']:.4f}", f"{n['change12h']*100:+.2f}%", f"{n['days']:.1f}d"))

        # 4. 12h Top (Bottom Tab 3 - Split)
        self.top_movers_12h.sort(key=lambda x: x['change'], reverse=True)
        
        # Gainers
        for item in self.tree_12h_up.get_children(): self.tree_12h_up.delete(item)
        for t in self.top_movers_12h[:10]: # Top 10 Gainers
            self.tree_12h_up.insert("", "end", values=(t['symbol'], f"{t['change']*100:+.2f}%", f"{t['price']:.4f}"))
            
        # Losers
        for item in self.tree_12h_down.get_children(): self.tree_12h_down.delete(item)
        for t in self.top_movers_12h[-10:]: # Bottom 10 Losers
            self.tree_12h_down.insert("", "end", values=(t['symbol'], f"{t['change']*100:+.2f}%", f"{t['price']:.4f}"))

    def get_klines(self, symbol):
        try:
            params = {'symbol': symbol, 'interval': '15m', 'limit': 50}
            resp = requests.get(f"{self.base_url}/fapi/v1/klines", params=params, proxies=self.proxies, verify=False, timeout=5)
            df = pd.DataFrame(resp.json(), columns=['op_t','o','h','l','c','v','cl_t','qav','nt','tb','tq','ig'])
            df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
            return df
        except: return None

if __name__ == "__main__":
    root = tk.Tk()
    app = Level1ScannerV05(root)
    root.mainloop()