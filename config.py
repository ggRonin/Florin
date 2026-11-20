# ======================= НАСТРОЙКИ FLORIN BOT =======================

RPC_URL = "https://rpc.testnet.chain.robinhood.com"
ACCOUNTS_FILE = "accounts.txt"     # мнемоника/приватник[,login:pass@host:port]
STATE_FILE = "data/state.json"
LOG_DIR = "logs"

USE_PROXY_FOR_RPC = False          # твои резидентные прокси блокируют RPC (503 Target host denied)
USE_PROXY_FOR_FAUCET = True        # клеймить кран через прокси аккаунта

PROXY_FILE = "Proxy.txt"           # пул запасных прокси
AUTO_REPLACE_PROXY = True          # нерабочий прокси -> берём из пула, удаляем оттуда, пишем в accounts.txt
                                   # (перед заменой проверяется интернет без прокси)

from settings import SLEEP_TIME, THREADS  # noqa: E402,F401  — потоки и сон задаются в settings.py

SHUFFLE_ACCOUNTS = True
SLEEP_WAKE_SPREAD_MIN = (0, 60)        # после сна аккаунты просыпаются вразнобой в этом окне (мин)

# ---------- тайминги ----------
DELAY_BETWEEN_ACTIONS = (25, 150)      # сек
DELAY_BETWEEN_ACCOUNTS = (60, 400)     # сек, только для `once`
START_SPREAD_MIN = (0, 90)             # первый запуск run: аккаунты стартуют вразнобой в этом окне (мин)

# ---------- faucet / расписание ----------
# Каждый аккаунт живёт по своему таймеру: сессия запускается, когда пора клеймить кран.
# Следующий клейм = последний клейм + 2ч + случайно 5..45 мин.
FAUCET_COOLDOWN = 2 * 3600
FAUCET_EXTRA_DELAY_MIN = (5, 45)
FAUCET_RETRY_AFTER_FAIL_MIN = (20, 45) # если кран упал не из-за лимита

# ---------- индивидуальность аккаунтов ----------
# Каждому аккаунту один раз генерируется «профиль» (хранится в data/state.json):
# свой диапазон кол-ва действий, свои веса действий, часть действий может быть ему «не интересна».
ACTIONS_PER_SESSION = (1, 7)           # общие границы
PROFILE_WEIGHT_SPREAD = (0.2, 1.8)     # множитель к весу действия
PROFILE_SKIP_CHANCE = 0.15             # шанс, что аккаунт вообще не делает какое-то второстепенное действие
SESSION_SKIP_CHANCE = 0.07             # шанс в сессии только заклеймить кран и ничего не делать

# ---------- Borrow ----------
MAX_BORROW_POSITIONS = 2
BORROW_COLL_SHARE = (0.15, 0.35)       # доля SPY с кошелька в залог
BORROW_LTV = (0.25, 0.50)              # LTV при открытии (MCR=110%, т.е. макс 90.9%)
INTEREST_RATE = (5.0, 12.0)            # % годовых. Выше средней (~6.9%) -> нас реже редемптят
                                       # (редемпшен гасит долг низкоставочных позиций и режет наш объём)
RATE_ADJ_COOLDOWN_H = 24               # не менять ставку чаще (раньше 7 дней — небольшая комиссия)

# ---------- Multiply (главная новая фича, которую продвигает команда) ----------
MAX_MULTIPLY_POSITIONS = 2
MULTIPLY_DEPOSIT_SHARE = (0.15, 0.35)
MULTIPLY_X = (1.5, 3.0)                # плечо при открытии / ребалансе (3x = CR 150%)

# ---------- Manage ----------
MIN_ICR_AFTER_ACTION = 1.6             # не опускать CR ниже 160% (borrow more / withdraw coll)
ADD_COLL_SHARE = (0.10, 0.40)          # доля SPY с кошелька (SPY с крана сразу в работу -> TVL)
WITHDRAW_COLL_SHARE = (0.10, 0.40)     # доля от свободного коллатерала
BORROW_MORE_SHARE = (0.10, 0.35)       # доля от доступного долга
REPAY_SHARE = (0.10, 0.40)             # доля от FUSD на кошельке

# ---------- Earn (Stability Pool) ----------
SP_DEPOSIT_SHARE = (0.30, 0.80)        # доля FUSD с кошелька
SP_WITHDRAW_SHARE = (0.10, 0.40)
SP_MIN_DEPOSIT = 20                    # FUSD

# ---------- вероятности действий (веса) ----------
# Логика: команда меряет успех объёмом "FUSD borrowed" и продвигает Multiply ->
# больше открытий/плеча/долга, позиции держим (TVL), закрытия и выводы — редко, «для живости».
WEIGHTS = {
    "open_borrow": 28,
    "open_multiply": 22,
    "sp_deposit": 22,
    "sp_withdraw": 4,
    "sp_claim": 14,                    # клейм наград Stability Pool (FUSD yield + SPY с ликвидаций)
    "add_coll": 16,
    "withdraw_coll": 3,
    "borrow_more": 16,
    "repay": 6,
    "set_rate": 7,
    "lever": 12,
    "close_borrow": 1,
    "close_multiply": 1,
}
PROFILE_VERSION = 2                    # поменяй число -> у всех аккаунтов пересоздадутся профили

# Если у аккаунта нет ни одной позиции — первым действием открывать Borrow
FIRST_OPEN_BORROW = True
