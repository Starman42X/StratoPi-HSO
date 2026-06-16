"""Re-export for ground station bundle (keep in sync with repo root e22_regs.py)."""
from __future__ import annotations

TX_POWER_DBM = (22, 17, 13, 10)
DBM_TO_REG = {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}
REG_TO_DBM = {v: k for k, v in DBM_TO_REG.items()}
DEFAULT_TX_DBM = 10
REG3_RSSI_ENABLE = 0x80


def power_reg(dbm: int) -> int:
    if dbm not in DBM_TO_REG:
        raise ValueError(f"TX power must be one of {TX_POWER_DBM} dBm, got {dbm}")
    return DBM_TO_REG[dbm]


def erp_mw(dbm: int) -> float:
    return round(10 ** (dbm / 10.0), 2)


def rssi_byte_to_dbm(value: int) -> float:
    return -(256 - (value & 0xFF))


def rssi_to_quality(dbm: float) -> int:
    return max(0, min(100, int((dbm + 120) * 100 / 70)))


def rssi_label(dbm: float) -> str:
    if dbm >= -70:
        return "Excellent"
    if dbm >= -90:
        return "Good"
    if dbm >= -105:
        return "Fair"
    if dbm >= -115:
        return "Weak"
    return "Poor"