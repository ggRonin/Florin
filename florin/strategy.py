"""Стратегия фарма: одна «сессия» на аккаунт = кран + несколько случайных действий."""
import random
import time

import config as cfg

from . import contracts as C
from .chain import E18, TxError, Wallet, fw, to_wei
from .protocol import Florin
from .utils import AccLog, Acc, ProxyManager, State, claim_faucet, now, sleep_left

PROXIES = ProxyManager(cfg.ACCOUNTS_FILE, cfg.PROXY_FILE)


def rnd(r):
    return random.uniform(*r)


def rand_rate(rng=None) -> int:
    r = to_wei(round(rnd(rng or cfg.INTEREST_RATE), 2) / 100)
    return max(C.MIN_RATE, min(C.MAX_RATE, r))


def pick(weights: dict) -> str:
    names, w = zip(*weights.items())
    return random.choices(names, weights=w, k=1)[0]


class Session:
    def __init__(self, acc: Acc, state: State):
        self.acc = acc
        self.state = state
        self.st = state.acc(acc.address)
        self.log = AccLog(acc.idx, acc.address)
        rpc_proxy = acc.proxy if cfg.USE_PROXY_FOR_RPC else None
        self.w = Wallet(acc.key, cfg.RPC_URL, rpc_proxy, self.log)
        self.f = Florin(self.w, self.log, self.st)

    # ---------------- снапшот ----------------
    def refresh(self):
        self.bal = self.f.balances()
        self.price = self.f.price()
        self.paused = self.f.paused()
        self.troves = self.f.my_troves()
        self.sp = self.f.sp_info()
        self.log.debug(f"snapshot: price={self.price / E18:.2f} paused={self.paused} bal={self.bal} sp={self.sp} "
                       f"troves={[(str(t['id'])[-6:], t['kind'], fw(t['coll']), fw(t['debt'], 0), t['rate'] / 1e16) for t in self.troves]}")
        self.state.save()

    def _by_kind(self, kind):
        return [t for t in self.troves if t["kind"] == kind]

    def summary(self) -> str:
        debt = sum(t["debt"] for t in self.troves)
        return (f"ETH {fw(self.bal['eth'], 5)} | SPY {fw(self.bal['spy'], 3)} | FUSD {fw(self.bal['fusd'], 2)} | "
                f"позиций {len(self.troves)} (долг {fw(debt, 0)}) | SP {fw(self.sp['deposit'], 2)}"
                f"{' | РЫНОК НА ПАУЗЕ' if self.paused else ''}")

    # ---------------- профиль аккаунта ----------------
    CORE = {"open_borrow", "open_multiply", "borrow_more", "add_coll", "sp_deposit", "sp_claim"}

    def profile(self) -> dict:
        p = self.st.get("profile")
        if p and set(p.get("weights", {})) == set(cfg.WEIGHTS) and p.get("v") == cfg.PROFILE_VERSION:
            return p
        lo_b, hi_b = cfg.ACTIONS_PER_SESSION
        lo = random.randint(lo_b, max(lo_b, (lo_b + hi_b) // 2))
        hi = random.randint(lo + 1, hi_b) if lo < hi_b else hi_b
        weights = {}
        for k, w in cfg.WEIGHTS.items():
            if k not in self.CORE and random.random() < cfg.PROFILE_SKIP_CHANCE:
                weights[k] = 0
            else:
                weights[k] = round(w * rnd(cfg.PROFILE_WEIGHT_SPREAD), 2)
        r_lo, r_hi = cfg.INTEREST_RATE
        c = rnd((r_lo, r_hi))
        p = {"v": cfg.PROFILE_VERSION, "actions": [lo, hi], "weights": weights,
             "rate": [round(max(r_lo, c - 2), 2), round(min(r_hi, c + 2), 2)]}
        self.st["profile"] = p
        self.state.save()
        self.log.info(f"создан профиль: действий {lo}-{hi}, ставка {p['rate'][0]}-{p['rate'][1]}%, "
                      f"отключены: {[k for k, v in weights.items() if v == 0] or '-'}")
        return p

    # ---------------- faucet ----------------
    def faucet_next(self) -> int:
        return int(self.st.get("faucet_next", 0))

    def _schedule_faucet(self, base: int, cooldown: int):
        extra = int(rnd(cfg.FAUCET_EXTRA_DELAY_MIN) * 60)
        self.st["faucet_next"] = base + cooldown + extra
        nxt = time.strftime("%H:%M", time.localtime(self.st["faucet_next"]))
        self.log.info(f"следующий клейм крана в {nxt}")

    def faucet(self, force=False) -> bool:
        if not force and now() < self.faucet_next():
            return False
        proxy = None
        if cfg.USE_PROXY_FOR_FAUCET:
            proxy = PROXIES.ensure(self.acc, self.log) if cfg.AUTO_REPLACE_PROXY else self.acc.proxy
        ok, msg = claim_faucet(self.acc.address, proxy)
        self.log.debug(f"faucet ответ: ok={ok} msg={msg} proxy=…@{(proxy or 'direct').rsplit('@', 1)[-1]}")
        if not ok and msg.startswith("сеть/прокси") and cfg.USE_PROXY_FOR_FAUCET and cfg.AUTO_REPLACE_PROXY:
            proxy = PROXIES.ensure(self.acc, self.log)
            ok, msg = claim_faucet(self.acc.address, proxy)
        self.st["faucet_try"] = now()
        if ok:
            self.st["faucet_ok"] = now()
            self.st.setdefault("stats", {})
            self.st["stats"]["faucet"] = self.st["stats"].get("faucet", 0) + 1
            self.log.success("FAUCET: запрос принят")
            self._schedule_faucet(now(), cfg.FAUCET_COOLDOWN)
        elif "too many" in msg.lower() or "hour" in msg.lower():
            self.log.warning(f"FAUCET: {msg}")
            self._schedule_faucet(now(), cfg.FAUCET_COOLDOWN)
        else:
            self.log.warning(f"FAUCET: {msg}")
            self.st["faucet_next"] = now() + int(rnd(cfg.FAUCET_RETRY_AFTER_FAIL_MIN) * 60)
        self.state.save()
        return ok

    # ---------------- доступность действий ----------------
    def _eth_ok(self):
        return self.bal["eth"] >= C.ETH_GAS_COMP + 3 * 10**14

    def _max_extra_debt(self, t):
        return int(t["coll"] * self.price / E18 / cfg.MIN_ICR_AFTER_ACTION) - t["debt"]

    def _free_coll(self, t):
        need = int(t["debt"] * cfg.MIN_ICR_AFTER_ACTION * E18 / self.price)
        return t["coll"] - need

    def available(self) -> dict:
        b, sp = self.bal, self.sp
        borrows, mults = self._by_kind("borrow"), self._by_kind("multiply")
        spy_usd = b["spy"] * self.price / E18 / E18
        ok = {}
        if not self.paused and self._eth_ok():
            if len(borrows) < cfg.MAX_BORROW_POSITIONS and spy_usd * 0.9 * cfg.BORROW_LTV[1] >= 1060:
                ok["open_borrow"] = True
            if len(mults) < cfg.MAX_MULTIPLY_POSITIONS and spy_usd * 0.9 * (cfg.MULTIPLY_X[1] - 1) >= 1100:
                ok["open_multiply"] = True
        if b["fusd"] >= cfg.SP_MIN_DEPOSIT * E18:
            ok["sp_deposit"] = True
        if sp["deposit"] > 2 * E18:
            ok["sp_withdraw"] = True
        if sp["deposit"] > 0 and (sp["yield"] > E18 // 100 or sp["coll"] > 0) or (sp["deposit"] == 0 and sp["stashed"] > 0):
            ok["sp_claim"] = True
        if self.troves and b["spy"] > E18 // 20:
            ok["add_coll"] = True
        if not self.paused:
            if any(self._free_coll(t) > E18 // 100 for t in borrows):
                ok["withdraw_coll"] = True
            if any(self._max_extra_debt(t) > 20 * E18 for t in borrows):
                ok["borrow_more"] = True
            if mults:
                ok["lever"] = True
                ok["close_multiply"] = True
        if b["fusd"] > 10 * E18 and any(t["debt"] > C.MIN_DEBT + 20 * E18 for t in borrows):
            ok["repay"] = True
        cd = cfg.RATE_ADJ_COOLDOWN_H * 3600
        if any(now() - t["last_rate_adj"] > cd and int(t["batch"], 16) == 0 for t in borrows):
            ok["set_rate"] = True
        if any(b["fusd"] >= t["debt"] for t in borrows) and len(self.troves) > 1:
            ok["close_borrow"] = True
        self._feasible = set(ok)
        w = self.profile()["weights"]
        return {k: w.get(k, 0) for k in ok if w.get(k, 0) > 0}

    # ---------------- действия ----------------
    def a_open_borrow(self):
        spy = self.bal["spy"]
        ltv = rnd(cfg.BORROW_LTV)
        coll = int(spy * rnd(cfg.BORROW_COLL_SHARE))
        bold = coll * self.price // E18 * int(ltv * 1000) // 1000
        if bold < 1050 * E18:  # подтягиваем залог до минимума 1000 FUSD
            coll = int(1060 * E18 * E18 / (self.price * ltv)) + 1
            coll = min(coll, int(spy * 0.9))
            bold = coll * self.price // E18 * int(ltv * 1000) // 1000
        bold = bold // E18 * E18  # круглые FUSD
        self.f.open_borrow(coll, bold, rand_rate(self.profile()["rate"]))

    def a_open_multiply(self):
        m = round(rnd(cfg.MULTIPLY_X), 1)
        spy = self.bal["spy"]
        dep = int(spy * rnd(cfg.MULTIPLY_DEPOSIT_SHARE))
        need = int(1080 * E18 * E18 / (self.price * (m - 1))) + 1
        dep = min(max(dep, need), int(spy * 0.9))
        self.f.open_multiply(dep, m, rand_rate(self.profile()["rate"]))

    def a_sp_deposit(self):
        amt = int(self.bal["fusd"] * rnd(cfg.SP_DEPOSIT_SHARE)) // E18 * E18
        self.f.sp_deposit(max(amt, cfg.SP_MIN_DEPOSIT * E18), claim=False)

    def a_sp_withdraw(self):
        amt = int(self.sp["deposit"] * rnd(cfg.SP_WITHDRAW_SHARE)) // E18 * E18
        self.f.sp_withdraw(max(amt, E18), claim=False)

    def a_sp_claim(self):
        if self.sp["deposit"] == 0:
            self.f.sp_claim_coll()
        else:
            self.f.sp_withdraw(0, claim=True)

    def a_add_coll(self):
        t = random.choice(self.troves)
        amt = max(int(self.bal["spy"] * rnd(cfg.ADD_COLL_SHARE)), E18 // 50)
        self.f.add_coll(t, min(amt, self.bal["spy"]))

    def a_withdraw_coll(self):
        ts = [t for t in self._by_kind("borrow") if self._free_coll(t) > E18 // 100]
        t = random.choice(ts)
        self.f.withdraw_coll(t, int(self._free_coll(t) * rnd(cfg.WITHDRAW_COLL_SHARE)))

    def a_borrow_more(self):
        ts = [t for t in self._by_kind("borrow") if self._max_extra_debt(t) > 20 * E18]
        t = random.choice(ts)
        amt = int(self._max_extra_debt(t) * rnd(cfg.BORROW_MORE_SHARE)) // E18 * E18
        self.f.borrow_more(t, max(amt, 10 * E18))

    def a_repay(self):
        ts = [t for t in self._by_kind("borrow") if t["debt"] > C.MIN_DEBT + 20 * E18]
        t = random.choice(ts)
        amt = int(self.bal["fusd"] * rnd(cfg.REPAY_SHARE))
        amt = min(amt, t["debt"] - C.MIN_DEBT - E18) // E18 * E18
        if amt < E18:
            raise TxError("нечего гасить")
        self.f.repay(t, amt)

    def a_set_rate(self):
        cd = cfg.RATE_ADJ_COOLDOWN_H * 3600
        ts = [t for t in self._by_kind("borrow") if now() - t["last_rate_adj"] > cd and int(t["batch"], 16) == 0]
        t = random.choice(ts)
        rate = rand_rate(self.profile()["rate"])
        if abs(rate - t["rate"]) < 10**15:
            rate += 5 * 10**15
        self.f.set_rate(t, rate)

    def a_lever(self):
        t = random.choice(self._by_kind("multiply"))
        self.f.lever(t, round(rnd(cfg.MULTIPLY_X), 1))

    def a_close_borrow(self):
        ts = [t for t in self._by_kind("borrow") if self.bal["fusd"] >= t["debt"]]
        self.f.close(random.choice(ts))

    def a_close_multiply(self):
        self.f.close_from_collateral(random.choice(self._by_kind("multiply")))

    # ---------------- сессия ----------------
    def run(self, actions: int | None = None, only: str | None = None):
        prof = self.profile()
        if self.faucet():
            time.sleep(random.uniform(20, 60))  # ждём, пока кран отправит токены
        self.refresh()
        self.log.info(self.summary())
        if not only and not actions and random.random() < cfg.SESSION_SKIP_CHANCE:
            self.log.info("эта сессия — только кран (рандом)")
            return 0
        n = actions or random.randint(*prof["actions"])
        self.log.info(f"план: {n} действий")
        done = 0
        for i in range(n):
            if not only and sleep_left(cfg.SLEEP_TIME):
                self.log.info("наступило время сна — прерываю сессию")
                break
            avail = self.available()
            if only:
                name = only if only in self._feasible else None
                if not name:
                    self.log.warning(f"действие {only} сейчас недоступно")
                    break
            elif not avail:
                self.log.info("нет доступных действий (нужны токены с крана?)")
                break
            elif i == 0 and cfg.FIRST_OPEN_BORROW and not self.troves and "open_borrow" in avail:
                name = "open_borrow"
            else:
                name = pick(avail)
            self.log.debug(f"шаг {i + 1}/{n}: выбрано {name} из {avail}")
            try:
                getattr(self, "a_" + name)()
                done += 1
                self.st.setdefault("stats", {})
                self.st["stats"][name] = self.st["stats"].get(name, 0) + 1
            except TxError as e:
                self.log.warning(f"{name}: {e}")
                self.log.debug(f"{name}: TxError, доступные действия были {sorted(avail)}")
            except Exception as e:
                self.log.exception(f"{name}: {type(e).__name__}: {str(e)[:300]}")
            self.state.save()
            if i < n - 1:
                time.sleep(rnd(cfg.DELAY_BETWEEN_ACTIONS))
                self.refresh()
        return done
