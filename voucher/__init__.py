"""多展点惠民券券务后端。

领域规则与设计要点见模块级文档与 ``voucher.engine``。所有金额均为整数分。
"""

from .engine import Engine

__all__ = ["Engine"]
