from sqlmodel import SQLModel, Field
from datetime import datetime
from typing import Optional

# 扫描结果表 (对应 V0.5 的 tree_signal)
class ScanResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str
    price: float
    change_percent: float  # 对应 V0.5 的 change
    vol_ratio: float       # 对应 V0.5 的 vol_ratio
    rule_name: str         # 对应 V0.5 的 reason
    score: int             # 对应 V0.5 的 score
    evo_state: str         # 对应 V0.5 的 evo (🚀, ⚖️, 📉)
    tags: str              # 对应 V0.5 的 tags
    created_at: datetime = Field(default_factory=datetime.now)

# 系统日志表
class SystemLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    level: str
    message: str
    created_at: datetime = Field(default_factory=datetime.now)

# 系统状态表 (心跳)
class SystemStatus(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    last_heartbeat: datetime
    scan_count_today: int = 0
    scan_round: int = 0