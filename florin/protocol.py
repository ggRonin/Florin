"""Все действия на florin.so: Borrow, Multiply, Earn (Stability Pool), Manage."""
import math
import random

from web3 import Web3

from . import contracts as C
from .chain import E18, TxError, Wallet, cs, fw

ZERO = "0x0000000000000000000000000000000000000000"


def fee_ceiling(fee: int) -> int:
    """Как на сайте: fee + 10% + 1 wei."""
    return 0 if fee == 0 else fee + fee // 10 + 1


class Florin:
    def __init__(self, wallet: Wallet, log, acc_state: dict):
        self.w = wallet
        self.log = log
        self.st = acc_state  # dict из state.json для этого адреса
        self.st.setdefault("troves", [])
        self.st.setdefault("scan_block", C.NFT_SCAN_FROM_BLOCK)

    @property
    def me(self):
        return self.w.address

    # ================= чтение =================
    def price(self) -> int:
        price, failed = self.w.call(C.SPY_PRICE_FEED, "fetchPrice()", out=["uint256", "bool"])
        if failed or price == 0:
            raise TxError("SPY oracle failure")
        return price

    def paused(self) -> bool:
        try:
            return self.w.call(C.SPY_PRICE_FEED, "isStockPaused()", out=["bool"])
        except Exception:
            return False

    def erc20_balance(self, token) -> int:
        return self.w.call(token, "balanceOf(address)", [self.me], ["uint256"])

    def balances(self) -> dict:
        return {
            "eth": self.w.eth_balance(),
            "spy": self.erc20_balance(C.SPY),
            "fusd": self.erc20_balance(C.FUSD),
        }

    def sp_info(self) -> dict:
        sp = C.STABILITY_POOL
        return {
            "deposit": self.w.call(sp, "getCompoundedBoldDeposit(address)", [self.me], ["uint256"]),
            "yield": self.w.call(sp, "getDepositorYieldGainWithPending(address)", [self.me], ["uint256"]),
            "coll": self.w.call(sp, "getDepositorCollGain(address)", [self.me], ["uint256"]),
            "stashed": self.w.call(sp, "stashedColl(address)", [self.me], ["uint256"]),
        }

    def trove(self, tid: int) -> dict | None:
        t = self.w.call(C.TROVE_MANAGER, "Troves(uint256)", [tid],
                        ["uint256", "uint256", "uint256", "uint8", "uint64", "uint64", "uint64",
                         "uint256", "address", "uint256"])
        status = t[3]
        if status != 1:  # 1 = active
            return None
        d = self.w.call(C.TROVE_MANAGER, "getLatestTroveData(uint256)", [tid],
                        ["(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"])
        _, receiver = self.w.call(C.BORROWER_OPS, "removeManagerReceiverOf(uint256)", [tid],
                                  ["address", "address"])
        return {
            "id": tid,
            "debt": d[0],
            "coll": d[1],
            "rate": d[6],
            "last_rate_adj": d[9],
            "batch": t[8],
            "kind": "multiply" if receiver.lower() == C.LEVERAGE_ZAPPER.lower() else "borrow",
        }

    def _scan_nft(self):
        """Ищем все TroveNFT, когда-либо пришедшие на адрес (в т.ч. открытые руками на сайте)."""
        latest = self.w.w3.eth.block_number
        start = int(self.st["scan_block"])
        topic_to = "0x" + "0" * 24 + self.me[2:].lower()
        found = set(self.st["troves"])
        while start <= latest:
            end = min(start + C.NFT_SCAN_CHUNK, latest)
            logs = self.w._retry(lambda: self.w.w3.eth.get_logs({
                "address": cs(C.TROVE_NFT), "fromBlock": start, "toBlock": end,
                "topics": [C.TRANSFER_TOPIC, None, topic_to]}))
            for lg in logs:
                found.add(str(int(Web3.to_hex(lg["topics"][3]), 16)))
            start = end + 1
        self.st["scan_block"] = latest - 50
        self.st["troves"] = sorted(found)

    def my_troves(self) -> list[dict]:
        try:
            self._scan_nft()
        except Exception as e:
            self.log.warning(f"скан TroveNFT не удался: {e}")
        res, alive = [], []
        for tid_s in self.st["troves"]:
            tid = int(tid_s)
            try:
                owner = self.w.call(C.TROVE_NFT, "ownerOf(uint256)", [tid], ["address"])
            except Exception:
                continue  # сожжён (позиция закрыта)
            if owner.lower() != self.me.lower():
                continue
            t = self.trove(tid)
            if t:
                res.append(t)
                alive.append(tid_s)
        self.st["troves"] = alive
        return res

    def icr(self, coll: int, debt: int, price: int) -> float:
        return (coll * price / E18) / debt if debt else 99.0

    # ================= hints / fees =================
    def hints(self, rate: int) -> tuple[int, int]:
        size = self.w.call(C.SORTED_TROVES, "size()", out=["uint256"])
        if size == 0:
            return 0, 0
        trials = min(max(int(math.ceil(15 * math.sqrt(size))), 1), 150)
        seed = random.getrandbits(64)
        best = None
        while trials > 0:
            n = min(50, trials)
            hid, diff, seed = self.w.call(C.HINT_HELPERS, "getApproxHint(uint256,uint256,uint256,uint256)",
                                          [0, rate, n, seed], ["uint256", "uint256", "uint256"])
            if best is None or diff < best[1]:
                best = (hid, diff)
            trials -= n
        up, low = self.w.call(C.SORTED_TROVES, "findInsertPosition(uint256,uint256,uint256)",
                              [rate, best[0], best[0]], ["uint256", "uint256"])
        return up, low

    def _open_fee(self, bold: int, rate: int) -> int:
        return self.w.call(C.HINT_HELPERS, "predictOpenTroveUpfrontFee(uint256,uint256,uint256)",
                           [0, bold, rate], ["uint256"])

    def _ensure_allowance(self, token: str, spender: str, amount: int):
        cur = self.w.call(token, "allowance(address,address)", [self.me, spender], ["uint256"])
        if cur >= amount:
            return
        _, h = self.w.send(token, "approve(address,uint256)", [cs(spender), amount])
        self.log.info(f"approve {fw(amount)} -> {spender[:10]} | {h}")

    @staticmethod
    def _minted_trove(rcpt) -> int | None:
        for lg in rcpt["logs"]:
            if lg["address"].lower() == C.TROVE_NFT.lower() and len(lg["topics"]) == 4:
                if Web3.to_hex(lg["topics"][0]) == C.TRANSFER_TOPIC and int(Web3.to_hex(lg["topics"][1]), 16) == 0:
                    return int(Web3.to_hex(lg["topics"][3]), 16)
        return None

    def _remember(self, tid):
        if tid is not None and str(tid) not in self.st["troves"]:
            self.st["troves"].append(str(tid))

    # ================= BORROW =================
    def open_borrow(self, coll: int, bold: int, rate: int) -> int:
        if bold < C.MIN_DEBT:
            raise TxError("Borrow at least 1,000 FUSD")
        if self.w.eth_balance() < C.ETH_GAS_COMP + 2 * 10**14:
            raise TxError("нужно ≥0.0012 ETH (0.001 залог за газ + комиссия)")
        price = self.price()
        fee = self._open_fee(bold, rate)
        if self.icr(coll, bold + fee, price) < C.MCR + 0.05:
            raise TxError("слишком высокий LTV")
        up, low = self.hints(rate)
        permit = self.w.sign_permit(C.SPY, C.GAS_COMP_ZAPPER, coll)
        params = (self.me, random.getrandbits(64), coll, bold, up, low, rate, ZERO,
                  fee_ceiling(fee), ZERO, ZERO, ZERO)
        rcpt, h = self.w.send(
            C.GAS_COMP_ZAPPER,
            "openTroveWithRawETHPermit((uint256,uint256,uint8,bytes32,bytes32),"
            "(address,uint256,uint256,uint256,uint256,uint256,uint256,address,uint256,address,address,address))",
            [permit, params], value=C.ETH_GAS_COMP)
        tid = self._minted_trove(rcpt)
        self._remember(tid)
        self.log.success(f"BORROW: {fw(coll)} SPY -> {fw(bold, 2)} FUSD @ {rate / 1e16:.2f}% | {h}")
        return tid

    # ================= MULTIPLY =================
    def multiply_quote(self, flash: int) -> int:
        """FUSD, который нужно занять, чтобы купить flash SPY (как N._ на сайте)."""
        ex = C.MULTIPLY_EXCHANGE
        feed = self.w.call(ex, "priceFeed()", out=["address"])
        p_scale = self.w.call(ex, "priceScale()", out=["uint256"])
        o_scale = self.w.call(ex, "oracleScale()", out=["uint256"])
        answer = self.w.call(feed, "latestRoundData()", out=["uint80", "int256", "uint256", "uint256", "uint80"])[1]
        px = answer * p_scale
        quoted = (flash * px + o_scale - 1) // o_scale
        return (quoted * 10050 + 9999) // 10000  # +0.5%

    def open_multiply(self, deposit: int, mult: float, rate: int) -> int:
        if self.w.eth_balance() < C.ETH_GAS_COMP + 2 * 10**14:
            raise TxError("нужно ≥0.0012 ETH")
        bps = int(mult * 10000)
        flash = deposit * (bps - 10000) // 10000
        bold = self.multiply_quote(flash)
        if bold < C.MIN_DEBT:
            raise TxError(f"Multiply: долг {fw(bold, 0)} < 1000 FUSD, увеличь депозит/плечо")
        price = self.price()
        fee = self._open_fee(bold, rate)
        if self.icr(deposit + flash, bold + fee, price) < C.MCR + 0.05:
            raise TxError("Multiply: слишком большое плечо")
        self._ensure_allowance(C.SPY, C.LEVERAGE_ZAPPER, deposit)
        up, low = self.hints(rate)
        params = (self.me, random.getrandbits(64), deposit, flash, bold, up, low, rate, ZERO,
                  fee_ceiling(fee), ZERO, ZERO, ZERO)
        rcpt, h = self.w.send(
            C.LEVERAGE_ZAPPER,
            "openLeveragedTroveWithSPY((address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,"
            "address,uint256,address,address,address))",
            [params], value=C.ETH_GAS_COMP)
        tid = self._minted_trove(rcpt)
        self._remember(tid)
        self.log.success(f"MULTIPLY {mult:.2f}x: {fw(deposit)} SPY, долг {fw(bold, 2)} FUSD | {h}")
        return tid

    def lever(self, t: dict, target_mult: float):
        """leverUp / leverDown для multiply-позиции (формула j() с сайта)."""
        price = self.price()
        coll, debt = t["coll"], t["debt"]
        equity = coll * price // E18 - debt
        if equity <= 0:
            raise TxError("нет equity")
        n = int(target_mult * 10000)
        after = n * equity * E18 // (10000 * price)
        if after > coll:
            flash = after - coll
            bold = ((flash * price + E18 - 1) // E18 * 10050 + 9999) // 10000
            fee = self.w.call(C.HINT_HELPERS, "predictAdjustTroveUpfrontFee(uint256,uint256,uint256)",
                              [0, t["id"], bold], ["uint256"])
            if self.icr(after, debt + bold + fee, price) < C.MCR + 0.05:
                raise TxError("leverUp: слишком большое плечо")
            _, h = self.w.send(C.LEVERAGE_ZAPPER, "leverUpTrove((uint256,uint256,uint256,uint256))",
                               [(t["id"], flash, bold, fee_ceiling(fee))])
            self.log.success(f"LEVER UP -> {target_mult:.2f}x trove …{str(t['id'])[-6:]} | {h}")
        elif after < coll:
            flash = coll - after
            min_bold = flash * price // E18 * 9950 // 10000
            if debt - min_bold < C.MIN_DEBT:
                raise TxError("leverDown: долг станет < 1000")
            _, h = self.w.send(C.LEVERAGE_ZAPPER, "leverDownTrove((uint256,uint256,uint256))",
                               [(t["id"], flash, min_bold)])
            self.log.success(f"LEVER DOWN -> {target_mult:.2f}x trove …{str(t['id'])[-6:]} | {h}")

    def close_from_collateral(self, t: dict):
        price = self.price()
        flash = ((t["debt"] * E18 + price - 1) // price * 10050 + 9999) // 10000
        if flash >= t["coll"]:
            raise TxError("коллатерала не хватает на закрытие")
        min_coll = (t["coll"] - flash) * 9950 // 10000
        _, h = self.w.send(C.LEVERAGE_ZAPPER, "closeTroveFromCollateral(uint256,uint256,uint256)",
                           [t["id"], flash, min_coll])
        self.log.success(f"CLOSE (from collateral) trove …{str(t['id'])[-6:]} | {h}")

    # ================= MANAGE (BorrowerOperations, вызывает владелец) =================
    def add_coll(self, t: dict, amount: int):
        self._ensure_allowance(C.SPY, C.BORROWER_OPS, amount)
        _, h = self.w.send(C.BORROWER_OPS, "addColl(uint256,uint256)", [t["id"], amount])
        self.log.success(f"ADD COLL {fw(amount)} SPY | {h}")

    def withdraw_coll(self, t: dict, amount: int):
        _, h = self.w.send(C.BORROWER_OPS, "withdrawColl(uint256,uint256)", [t["id"], amount])
        self.log.success(f"WITHDRAW COLL {fw(amount)} SPY | {h}")

    def borrow_more(self, t: dict, amount: int):
        fee = self.w.call(C.HINT_HELPERS, "predictAdjustTroveUpfrontFee(uint256,uint256,uint256)",
                          [0, t["id"], amount], ["uint256"])
        _, h = self.w.send(C.BORROWER_OPS, "withdrawBold(uint256,uint256,uint256)",
                           [t["id"], amount, fee_ceiling(fee)])
        self.log.success(f"BORROW MORE {fw(amount, 2)} FUSD | {h}")

    def repay(self, t: dict, amount: int):
        _, h = self.w.send(C.BORROWER_OPS, "repayBold(uint256,uint256)", [t["id"], amount])
        self.log.success(f"REPAY {fw(amount, 2)} FUSD | {h}")

    def set_rate(self, t: dict, rate: int):
        up, low = self.hints(rate)
        fee = self.w.call(C.HINT_HELPERS, "predictAdjustInterestRateUpfrontFee(uint256,uint256,uint256)",
                          [0, t["id"], rate], ["uint256"])
        _, h = self.w.send(C.BORROWER_OPS,
                           "adjustTroveInterestRate(uint256,uint256,uint256,uint256,uint256)",
                           [t["id"], rate, up, low, fee_ceiling(fee)])
        self.log.success(f"RATE {t['rate'] / 1e16:.2f}% -> {rate / 1e16:.2f}% | {h}")

    def close(self, t: dict):
        _, h = self.w.send(C.BORROWER_OPS, "closeTrove(uint256)", [t["id"]])
        self.log.success(f"CLOSE trove …{str(t['id'])[-6:]} (долг {fw(t['debt'], 2)}) | {h}")

    # ================= EARN (Stability Pool) =================
    def sp_deposit(self, amount: int, claim: bool = True):
        _, h = self.w.send(C.STABILITY_POOL, "provideToSP(uint256,bool)", [amount, claim])
        self.log.success(f"EARN deposit {fw(amount, 2)} FUSD | {h}")

    def sp_withdraw(self, amount: int, claim: bool = True):
        _, h = self.w.send(C.STABILITY_POOL, "withdrawFromSP(uint256,bool)", [amount, claim])
        what = "claim" if amount == 0 else f"withdraw {fw(amount, 2)} FUSD"
        self.log.success(f"EARN {what} | {h}")

    def sp_claim_coll(self):
        _, h = self.w.send(C.STABILITY_POOL, "claimAllCollGains()")
        self.log.success(f"EARN claim SPY gains | {h}")
