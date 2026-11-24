"""老果园更新：树龄收益证据、跨年现金缺口与苗木订位。

仅使用 Python 标准库。模块划分：

* packet    资料读取、amendments 修订与引用/口径校验
* evidence  可查支撑范围的树龄收益证据表（含待补证标记）
* plans     保留旧树/立即更新/分年更新方案枚举
* engine    同年景下的逐年现金、借款与联保占用模拟
* allocate  起苗期限、库存与联保约束下的订位分配与复议
* report    面向果农与经办人的可读/可机读输出
"""

__all__ = [
    "packet",
    "evidence",
    "plans",
    "engine",
    "allocate",
    "report",
]
