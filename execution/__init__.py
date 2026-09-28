"""
Execution helpers.
"""

__all__ = [
    "Broker",
    "OrderManager",
    "PositionManager",
    "QmtMiniOrderGateway",
    "RiskController",
]


def __getattr__(name):
    if name == "Broker":
        from .broker import Broker

        return Broker
    if name == "OrderManager":
        from .order_manager import OrderManager

        return OrderManager
    if name == "PositionManager":
        from .position_manager import PositionManager

        return PositionManager
    if name == "RiskController":
        from .risk_controller import RiskController

        return RiskController
    if name == "QmtMiniOrderGateway":
        from .qmtmini_gateway import QmtMiniOrderGateway

        return QmtMiniOrderGateway
    raise AttributeError(name)
