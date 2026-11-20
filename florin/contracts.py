"""Адреса и константы Florin (Liquity V2 fork) на Robinhood Chain Testnet.
Взяты из фронтенда florin.so и проверены on-chain."""
from web3 import Web3

CHAIN_ID = 46630
EXPLORER = "https://explorer.testnet.chain.robinhood.com"

SPY = "0x8823b40A23387Df76E127744FFB38b26eB012A5c"           # коллатерал (ERC20 + permit)
FUSD = "0x1f93de989cd9d30b2c31f3227769632729858196"          # стейбл (boldToken)
WETH = "0x7943e237c7F95DA44E0301572D358911207852Fa"
BORROWER_OPS = "0xfea06161dbd14374ee6a746eae3502a1a6741990"
TROVE_MANAGER = "0xc1ebb33678176d7fb5c8fef3ba5a610d50bacc63"
TROVE_NFT = "0xe361393f4d0f3b40eca34c7a83e27e4b5dcfa1c3"
STABILITY_POOL = "0x9543b191c66c4fa68b9b7888df65b661842ace1b"
HINT_HELPERS = "0xcd8bf0cb142d8e612b9685772b2c5439240c1ba5"
SORTED_TROVES = "0x83311690476dfa9239b55e732b4b6638a15142df"
SPY_PRICE_FEED = "0xb7dc3a2596e9a0571b6e104d2f1a3d35b0653862"
GAS_COMP_ZAPPER = "0x90910002956b3C881B74910F5f144b6a73dCcFbe"  # Borrow (openTroveWithRawETHPermit)
LEVERAGE_ZAPPER = "0xf63a41A0C8fDB81704C92Ba39eAcfB11f8C3FD87"  # Multiply
MULTIPLY_EXCHANGE = "0x6AfC499c52571F03272Fa6a4FBba0b73674f7f4c"

FAUCET_URL = "https://faucet.florin.so/api/faucet"

MIN_DEBT = 1000 * 10**18
ETH_GAS_COMP = 10**15            # 0.001 ETH уходит в залог за ликвидацию при открытии позиции
MIN_RATE = 5 * 10**15            # 0.5%
MAX_RATE = 25 * 10**17           # 250%
MCR = 1.10

NFT_SCAN_FROM_BLOCK = 150_800_000  # примерно запуск Testnet V2
NFT_SCAN_CHUNK = 9_900_000

TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")

_err_names = [
    "TroveExists", "ICRBelowMCRPlusBCR", "DebtBelowMin", "ICRBelowMCR", "TCRBelowCCR",
    "UpfrontFeeTooHigh", "InterestRateTooLow", "InterestRateTooHigh", "NewOracleFailureDetected",
    "IsShutDown", "ZeroAdjustment", "CollWithdrawalTooHigh", "NotEnoughBoldBalance", "TroveInBatch",
    "InterestRateNotNew", "NotOwnerNorRemoveManager", "NotOwnerNorInterestManager", "TroveNotActive",
    "TroveNotOpen", "RepaymentNotMatchingCollWithdrawal", "IsStockPaused", "StockPaused",
    "ERC2612ExpiredSignature", "ERC2612InvalidSigner", "InsufficientAllowance", "InsufficientBalance",
]
ERRORS = {"0x" + Web3.keccak(text=f"{n}()")[:4].hex().removeprefix("0x"): n for n in _err_names}
