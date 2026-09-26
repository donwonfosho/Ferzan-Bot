// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface IWETH {
    function deposit() external payable;
    function transfer(address to, uint256 value) external returns (bool);
}

interface IV2Pair {
    function mint(address to) external returns (uint256 liquidity);
}

/// @notice Ferzan bonding curve (v2).
///
/// Pricing: constant product on virtual reserves (x = vEth + realEth, y = vToken - sold).
/// The virtual reserves are derived from the graduation target so that, when the curve
/// has raised exactly `gradTarget`:
///   - 80% of the curve's tokens have been sold,
///   - the remaining 20% seed the DEX pool together with all raised ETH,
///   - and the pool opens at EXACTLY the curve's final price (no drop for holders).
/// Any rounding dust of tokens is burned; the pool's LP tokens are burned (locked forever).
///
/// Fees: 1% of every buy and sell.
///   50% creator / 50% platform. If a valid referrer is passed, 10% of the fee goes to the
///   referrer out of the platform half (40 platform / 50 creator / 10 referrer).
///   If paying the creator or referrer fails, that share goes to the platform instead, so a
///   bad creator/referrer wallet can never freeze trading.
///
/// Safety:
///   - No owner, no admin, no upgrade, no pause. Parameters are immutable.
///   - Before graduation the token cannot be sent to the pool by anyone but this curve,
///     and graduation mints the pool directly (no router), so a pre-created or pre-funded
///     pool cannot make graduation fail.
///   - The buy that completes the curve only takes what is needed and refunds the rest.
contract FerzanCurve is ReentrancyGuard {
    uint256 public constant FEE_BPS = 100; // 1%
    uint256 public constant CREATOR_SHARE_BPS = 5000; // of the fee
    uint256 public constant REFERRER_SHARE_BPS = 1000; // of the fee, taken from the platform half
    uint256 public constant CURVE_SOLD_BPS = 8000; // 80% of curve tokens sold before graduation
    uint256 private constant PAY_GAS = 30_000; // gas for creator/referrer payouts
    uint256 private constant DUST_BPS = 1; // graduate when within 0.01% of the target
    address public constant DEAD = address(0xdead);

    address public immutable factory;
    IERC20 public immutable token;
    address public immutable creator;
    address public immutable platformTreasury;
    address public immutable weth;
    address public immutable pool;

    uint256 public immutable gradTarget; // net native raised that completes the curve
    uint256 public immutable virtualEth;
    uint256 public immutable virtualToken;
    uint256 public immutable curveSupply; // tokens this curve was seeded with
    uint256 public immutable startTime;
    uint256 public immutable maxBuyPerWallet; // gross native per wallet, 0 = no limit

    uint256 public realEth;
    uint256 public tokensSold;
    bool public graduated;
    mapping(address => uint256) public boughtNative;

    event Trade(
        address indexed trader,
        bool isBuy,
        uint256 nativeAmount,
        uint256 tokenAmount,
        uint256 fee,
        address referrer,
        uint256 realEthAfter,
        uint256 tokensSoldAfter
    );
    event Graduated(address indexed pool, uint256 nativeSeeded, uint256 tokensSeeded, uint256 tokensBurned, uint256 lpBurned);
    event FeeRedirected(address indexed intended, uint256 amount);

    constructor(
        address token_,
        address creator_,
        address platformTreasury_,
        address weth_,
        address pool_,
        uint256 curveSupply_,
        uint256 gradTarget_,
        uint256 startTime_,
        uint256 maxBuyPerWallet_
    ) {
        require(
            token_ != address(0) && creator_ != address(0) && platformTreasury_ != address(0)
                && weth_ != address(0) && pool_ != address(0),
            "zero"
        );
        require(gradTarget_ >= 1e15 && gradTarget_ <= 1e24, "grad target");
        // upper bound keeps the pool seed under the V2 pair's uint112 reserve limit
        require(curveSupply_ >= 1e18 && curveSupply_ <= 2.5e34, "supply");
        factory = msg.sender;
        token = IERC20(token_);
        creator = creator_;
        platformTreasury = platformTreasury_;
        weth = weth_;
        pool = pool_;
        curveSupply = curveSupply_;
        gradTarget = gradTarget_;
        startTime = startTime_;
        maxBuyPerWallet = maxBuyPerWallet_;
        // vEth = G/3  =>  vToken = S * 16/15, sold at graduation = 80% S, pool gets 20% S,
        // and pool price == final curve price. (see contract notes)
        virtualEth = gradTarget_ / 3;
        virtualToken = (curveSupply_ * 16) / 15;
    }

    // ------------------------------------------------------------ views --
    function ethReserve() public view returns (uint256) {
        return virtualEth + realEth;
    }

    function tokenReserve() public view returns (uint256) {
        return virtualToken - tokensSold;
    }

    /// Price of 1 whole token (1e18 units) in native wei, at the current point on the curve.
    function spotPrice() external view returns (uint256) {
        return (ethReserve() * 1e18) / tokenReserve();
    }

    /// Progress to graduation in basis points (10000 = graduates).
    function progressBps() external view returns (uint256) {
        if (graduated) return 10_000;
        return (realEth * 10_000) / gradTarget;
    }

    /// -> tokens out, gross native actually used (<= nativeIn), refund, fee.
    function quoteBuy(uint256 nativeIn)
        public
        view
        returns (uint256 tokensOut, uint256 grossUsed, uint256 refund, uint256 fee)
    {
        require(!graduated, "graduated");
        require(nativeIn > 0, "zero in");
        uint256 net = nativeIn - (nativeIn * FEE_BPS) / 10_000;
        uint256 room = gradTarget - realEth;
        grossUsed = nativeIn;
        if (net >= room) {
            net = room;
            grossUsed = _ceilDiv(net * 10_000, 10_000 - FEE_BPS);
            if (grossUsed > nativeIn) grossUsed = nativeIn;
        }
        fee = grossUsed - net;
        refund = nativeIn - grossUsed;
        uint256 x = ethReserve();
        uint256 y = tokenReserve();
        uint256 newY = _ceilDiv(x * y, x + net);
        tokensOut = y - newY;
    }

    /// -> native out (after fee), fee.
    function quoteSell(uint256 tokenIn) public view returns (uint256 nativeOut, uint256 fee) {
        require(!graduated, "graduated");
        require(tokenIn > 0 && tokenIn <= tokensSold, "amount");
        uint256 x = ethReserve();
        uint256 y = tokenReserve();
        uint256 newX = _ceilDiv(x * y, y + tokenIn);
        uint256 gross = x - newX;
        if (gross > realEth) gross = realEth;
        fee = (gross * FEE_BPS) / 10_000;
        nativeOut = gross - fee;
    }

    // ------------------------------------------------------------ trade --
    function buy(uint256 minTokensOut, address referrer) external payable nonReentrant returns (uint256) {
        require(block.timestamp >= startTime, "not open yet");
        return _buy(msg.sender, minTokensOut, referrer, true, msg.sender);
    }

    /// Creator's dev buy, only from the factory inside the launch tx. Allowed before the
    /// trading window opens; not counted against the max-buy limit.
    function devBuy(address to) external payable nonReentrant returns (uint256) {
        require(msg.sender == factory, "only factory");
        return _buy(to, 0, address(0), false, to);
    }

    function sell(uint256 tokenIn, uint256 minNativeOut, address referrer) external nonReentrant returns (uint256) {
        require(block.timestamp >= startTime, "not open yet");
        (uint256 out, uint256 fee) = quoteSell(tokenIn);
        require(out >= minNativeOut, "slippage");
        require(token.transferFrom(msg.sender, address(this), tokenIn), "transferFrom");
        uint256 gross = out + fee;
        tokensSold -= tokenIn;
        realEth -= gross;
        emit Trade(msg.sender, false, out, tokenIn, fee, referrer, realEth, tokensSold);
        _splitFee(fee, referrer, msg.sender);
        _send(msg.sender, out);
        return out;
    }

    function _buy(address to, uint256 minTokensOut, address referrer, bool capped, address refundTo)
        internal
        returns (uint256)
    {
        require(!graduated, "graduated");
        (uint256 tokensOut, uint256 grossUsed, uint256 refund, uint256 fee) = quoteBuy(msg.value);
        require(tokensOut > 0 && tokensOut >= minTokensOut, "slippage");
        if (capped && maxBuyPerWallet > 0) {
            require(boughtNative[to] + grossUsed <= maxBuyPerWallet, "max buy per wallet");
        }
        if (capped) boughtNative[to] += grossUsed;
        tokensSold += tokensOut;
        realEth += grossUsed - fee;
        require(token.transfer(to, tokensOut), "transfer");
        emit Trade(to, true, grossUsed, tokensOut, fee, referrer, realEth, tokensSold);
        _splitFee(fee, referrer, to);
        // graduate at the target, or when only dust is left (so a tiny leftover can never
        // leave the curve stuck with buys that would receive 0 tokens)
        if (realEth + (gradTarget * DUST_BPS) / 10_000 >= gradTarget) {
            _graduate();
        }
        if (refund > 0) _send(refundTo, refund);
        return tokensOut;
    }

    // ------------------------------------------------------ graduation --
    function _graduate() internal {
        graduated = true;
        uint256 ethSeed = realEth;
        uint256 bal = token.balanceOf(address(this));
        // tokens that match the final curve price: ethSeed * y / x
        uint256 tokSeed = (ethSeed * tokenReserve()) / ethReserve();
        // Rounding can ask for a few wei more than the curve holds; then seed everything it has.
        // (That can only make the pool price a hair higher, never lower, for holders.)
        if (tokSeed > bal) tokSeed = bal;
        realEth = 0;
        uint256 burn = bal - tokSeed;
        IWETH(weth).deposit{value: ethSeed}();
        require(IWETH(weth).transfer(pool, ethSeed), "weth");
        require(token.transfer(pool, tokSeed), "seed");
        uint256 lp = IV2Pair(pool).mint(DEAD);
        if (burn > 0) require(token.transfer(DEAD, burn), "burn");
        emit Graduated(pool, ethSeed, tokSeed, burn, lp);
    }

    // ------------------------------------------------------------- fees --
    function _splitFee(uint256 fee, address referrer, address trader) internal {
        if (fee == 0) return;
        uint256 toCreator = (fee * CREATOR_SHARE_BPS) / 10_000;
        uint256 toRef = 0;
        if (
            referrer != address(0) && referrer != creator && referrer != platformTreasury
                && referrer != trader && referrer != address(this)
        ) {
            toRef = (fee * REFERRER_SHARE_BPS) / 10_000;
        }
        uint256 toPlatform = fee - toCreator - toRef;
        if (!_tryPay(creator, toCreator)) toPlatform += toCreator;
        if (toRef > 0 && !_tryPay(referrer, toRef)) toPlatform += toRef;
        _send(platformTreasury, toPlatform);
    }

    function _tryPay(address to, uint256 amt) internal returns (bool ok) {
        if (amt == 0) return true;
        (ok,) = to.call{value: amt, gas: PAY_GAS}("");
        if (!ok) emit FeeRedirected(to, amt);
    }

    function _send(address to, uint256 amt) internal {
        if (amt == 0) return;
        (bool ok,) = to.call{value: amt}("");
        require(ok, "native send failed");
    }

    function _ceilDiv(uint256 a, uint256 b) internal pure returns (uint256) {
        return a == 0 ? 0 : (a - 1) / b + 1;
    }
}
