// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface ITrc20 {
    function transfer(address to, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);
    function balanceOf(address who) external view returns (uint256);
}

interface IWtrx {
    function deposit() external payable;
    function transfer(address to, uint256 value) external returns (bool);
}

interface IV2Pair {
    function mint(address to) external returns (uint256 liquidity);
}

interface IV2Factory {
    function getPair(address a, address b) external view returns (address);
    function createPair(address a, address b) external returns (address);
}

/// @notice Ferzan bonding curve for Tron (TRX native, graduates to SunSwap V2).
/// Same pricing and fees as the EVM FerzanCurve:
///   constant product on virtual reserves (x = vTrx + realTrx, y = vToken - sold), with vTrx = G/3 and
///   vToken = S*16/15, so at the graduation target G exactly 80% of the curve's coins are sold and the rest
///   seed the pool at the curve's final price. 1% fee on every buy and sell: 50% creator, 50% platform,
///   of which 10% of the fee goes to a referrer when one is passed.
///
/// Tron differences:
///   - Every launch is a cheap EIP-1167 clone, initialized once by the factory in the launch transaction.
///   - Opening a SunSwap pool costs a lot of energy, so it does not happen inside a buyer's trade. The buy that
///     reaches the target marks the curve `complete` (trading stops, the extra TRX is refunded) and then anyone
///     can call graduate(): it creates the pool, seeds it with all the raised TRX (minus `gradReward`, paid to
///     the caller to cover that energy) and the matching coins at the curve's final price, burns the LP tokens
///     and burns the leftover coins. Ferzan's server calls it automatically.
///   - No owner, no admin, no upgrade, no pause.
contract FerzanTronCurve {
    uint256 public constant FEE_BPS = 100;
    uint256 public constant CREATOR_SHARE_BPS = 5000;
    uint256 public constant REFERRER_SHARE_BPS = 1000;
    uint256 private constant PAY_ENERGY = 30_000; // energy for creator/referrer payouts
    uint256 private constant DUST_BPS = 1;        // complete when within 0.01% of the target
    address public constant DEAD = address(0xdead);

    address public factory;
    address public token;
    address public creator;
    address public platformTreasury;
    address public wtrx;
    address public dexFactory;
    address public pool;          // the SunSwap pair, set at graduation
    uint256 public gradTarget;    // net TRX (sun) raised that completes the curve
    uint256 public virtualEth;
    uint256 public virtualToken;
    uint256 public curveSupply;
    uint256 public startTime;
    uint256 public maxBuyPerWallet;
    uint256 public gradReward;    // sun paid to whoever calls graduate()

    uint256 public realEth;
    uint256 public tokensSold;
    bool public complete;
    bool public graduated;
    mapping(address => uint256) public boughtNative;

    bool private _initialized;
    uint256 private _lock = 1;

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
    event Completed(uint256 realEth, uint256 tokensSold);
    event Graduated(address indexed pool, uint256 nativeSeeded, uint256 tokensSeeded, uint256 tokensBurned, uint256 lpBurned);
    event FeeRedirected(address indexed intended, uint256 amount);

    modifier nonReentrant() {
        require(_lock == 1, "reentrant");
        _lock = 2;
        _;
        _lock = 1;
    }

    constructor() {
        _initialized = true; // lock the master copy
    }

    struct Init {
        address token;
        address creator;
        address platformTreasury;
        address wtrx;
        address dexFactory;
        uint256 curveSupply;
        uint256 gradTarget;
        uint256 startTime;
        uint256 maxBuyPerWallet;
        uint256 gradReward;
    }

    function initialize(Init calldata p) external {
        require(!_initialized, "initialized");
        require(
            p.token != address(0) && p.creator != address(0) && p.platformTreasury != address(0) && p.wtrx != address(0)
                && p.dexFactory != address(0),
            "zero"
        );
        require(p.gradTarget >= 1e6 && p.gradTarget <= 1e17, "grad target");
        require(p.curveSupply >= 1e6 && p.curveSupply <= 2.5e30, "supply");
        _initialized = true;
        _lock = 1;
        factory = msg.sender;
        token = p.token;
        creator = p.creator;
        platformTreasury = p.platformTreasury;
        wtrx = p.wtrx;
        dexFactory = p.dexFactory;
        curveSupply = p.curveSupply;
        gradTarget = p.gradTarget;
        startTime = p.startTime;
        maxBuyPerWallet = p.maxBuyPerWallet;
        gradReward = p.gradReward;
        virtualEth = p.gradTarget / 3;
        virtualToken = (p.curveSupply * 16) / 15;
    }

    // ------------------------------------------------------------ views --
    function ethReserve() public view returns (uint256) {
        return virtualEth + realEth;
    }

    function tokenReserve() public view returns (uint256) {
        return virtualToken - tokensSold;
    }

    /// Price of 1 whole coin (1e6 units) in sun.
    function spotPrice() external view returns (uint256) {
        return (ethReserve() * 1e6) / tokenReserve();
    }

    function progressBps() external view returns (uint256) {
        if (complete) return 10_000;
        return (realEth * 10_000) / gradTarget;
    }

    function quoteBuy(uint256 nativeIn)
        public
        view
        returns (uint256 tokensOut, uint256 grossUsed, uint256 refund, uint256 fee)
    {
        require(!complete, "curve complete");
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

    function quoteSell(uint256 tokenIn) public view returns (uint256 nativeOut, uint256 fee) {
        require(!complete, "curve complete");
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
        return _buy(msg.sender, minTokensOut, referrer, true);
    }

    /// Creator's dev buy, only from the factory inside the launch transaction.
    function devBuy(address to) external payable nonReentrant returns (uint256) {
        require(msg.sender == factory, "only factory");
        return _buy(to, 0, address(0), false);
    }

    function sell(uint256 tokenIn, uint256 minNativeOut, address referrer) external nonReentrant returns (uint256) {
        require(block.timestamp >= startTime, "not open yet");
        (uint256 out, uint256 fee) = quoteSell(tokenIn);
        require(out >= minNativeOut, "slippage");
        require(ITrc20(token).transferFrom(msg.sender, address(this), tokenIn), "transferFrom");
        tokensSold -= tokenIn;
        realEth -= out + fee;
        emit Trade(msg.sender, false, out, tokenIn, fee, referrer, realEth, tokensSold);
        _splitFee(fee, referrer, msg.sender);
        _send(msg.sender, out);
        return out;
    }

    function _buy(address to, uint256 minTokensOut, address referrer, bool capped) internal returns (uint256) {
        (uint256 tokensOut, uint256 grossUsed, uint256 refund, uint256 fee) = quoteBuy(msg.value);
        require(tokensOut > 0 && tokensOut >= minTokensOut, "slippage");
        if (capped && maxBuyPerWallet > 0) {
            require(boughtNative[to] + grossUsed <= maxBuyPerWallet, "max buy per wallet");
        }
        if (capped) boughtNative[to] += grossUsed;
        tokensSold += tokensOut;
        realEth += grossUsed - fee;
        require(ITrc20(token).transfer(to, tokensOut), "transfer");
        emit Trade(to, true, grossUsed, tokensOut, fee, referrer, realEth, tokensSold);
        _splitFee(fee, referrer, to);
        if (realEth + (gradTarget * DUST_BPS) / 10_000 >= gradTarget) {
            complete = true;
            emit Completed(realEth, tokensSold);
        }
        if (refund > 0) _send(to, refund);
        return tokensOut;
    }

    // ------------------------------------------------------ graduation --
    /// Anyone can call this once the curve is complete; the caller receives `gradReward` for the energy.
    function graduate() external nonReentrant returns (address pair) {
        require(complete && !graduated, "not ready");
        graduated = true;
        // The coin cannot have reached the pair before now (it only moves through this curve until graduation),
        // so even a pair someone created early holds none of it; any TRX gifted into it only raises the price.
        pair = IV2Factory(dexFactory).getPair(token, wtrx);
        if (pair == address(0)) pair = IV2Factory(dexFactory).createPair(token, wtrx);
        pool = pair;

        uint256 raised = realEth;
        uint256 reward = gradReward;
        if (reward > raised / 20) reward = raised / 20; // never more than 5% of the pool
        uint256 ethSeed = raised - reward;
        uint256 bal = ITrc20(token).balanceOf(address(this));
        // coins that match the curve's final price for the TRX actually seeded
        uint256 tokSeed = (ethSeed * tokenReserve()) / ethReserve();
        if (tokSeed > bal) tokSeed = bal;
        realEth = 0;
        uint256 burn = bal - tokSeed;

        IWtrx(wtrx).deposit{value: ethSeed}();
        require(IWtrx(wtrx).transfer(pair, ethSeed), "wtrx");
        require(ITrc20(token).transfer(pair, tokSeed), "seed");
        uint256 lp = IV2Pair(pair).mint(DEAD);
        if (burn > 0) require(ITrc20(token).transfer(DEAD, burn), "burn");
        emit Graduated(pair, ethSeed, tokSeed, burn, lp);
        if (reward > 0) _send(msg.sender, reward);
    }

    // ------------------------------------------------------------- fees --
    function _splitFee(uint256 fee, address referrer, address trader) internal {
        if (fee == 0) return;
        uint256 toCreator = (fee * CREATOR_SHARE_BPS) / 10_000;
        uint256 toRef = 0;
        if (
            referrer != address(0) && referrer != creator && referrer != platformTreasury && referrer != trader
                && referrer != address(this)
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
        (ok,) = to.call{value: amt, gas: PAY_ENERGY}("");
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
