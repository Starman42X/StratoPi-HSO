"""EBYTE E22 register helpers for Waveshare SX1268 433M HAT (22 dBm module)."""

from __future__ import annotations

# REG1[1:0] on 22 dBm E22 modules (verified on StratoPi hardware)
TX_POWER_DBM = (22, 17, 13, 10)
DBM_TO_REG = {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}
REG_TO_DBM = {v: k for k, v in DBM_TO_REG.items()}

DEFAULT_TX_DBM = 10  # 10 dBm ~ 10 mW ERP (DE ISM limit)

# REG3 bit7: prepend one RSSI byte before UART RX payload
REG3_RSSI_ENABLE = 0x80


def power_reg(dbm: int) -> int:
    if dbm not in DBM_TO_REG:
        raise ValueError(f"TX power must be one of {TX_POWER_DBM} dBm, got {dbm}")
    return DBM_TO_REG[dbm]


def erp_mw(dbm: int) -> float:
    """Approximate ERP milliwatts (isotropic)."""
    return round(10 ** (dbm / 10.0), 2)


def rssi_byte_to_dbm(value: int) -> float:
    """E22 UART RSSI prefix byte to dBm."""
    return -(256 - (value & 0xFF))


def rssi_to_quality(dbm: float) -> int:
    """0–100 link quality (tuned for SF12 HAB links)."""
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