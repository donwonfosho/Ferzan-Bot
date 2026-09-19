// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IUniswapV2RouterMinimal, IERC20Minimal} from "./IUniswapV2RouterMinimal.sol";

/// Constant-product curve with virtual reserves.
/// 1% fee: 60% platform / 30% creator / 10% referrer (referrer share stays platform if address(0)).
/// Graduation seeds a V2 pool and burns LP to address(0).
contract BondingCurve is ReentrancyGuard {
    uint256 public constant FEE_BPS = 100;
    uint256 public constant PLATFORM_SHARE_BPS = 6000;
    uint256 public constant CREATOR_SHARE_BPS = 3000;
    uint256 public constant REFERRER_SHARE_BPS = 1000;
    address public constant DEAD = address(0xdead);

    address public immutable token;
    address public immutable creator;
    address public immutable platformTreasury;
    IUniswapV2RouterMinimal public immutable router;

    uint256 public immutable virtualEth;
    uint256 public immutable virtualToken;
    uint256 public immutable graduationEthThreshold;
    uint256 public immutable startTime;
    uint256 public immutable maxBuyPerWallet;

    uint256 public realEth;
    uint256 public tokensSold;
    bool public graduated;
    mapping(address => uint256) public boughtNative;

    event Buy(address indexed buyer, uint256 ethIn, uint256 tokensOut, uint256 fee, address referrer);
    event Sell(address indexed seller, uint256 tokensIn, uint256 ethOut, uint256 fee);
    event Graduated(uint256 ethSeeded, uint256 tokensSeeded, uint256 lpBurned);

    constructor(
        address token_,
        address creator_,
        address platformTreasury_,
        address router_,
        uint256 virtualEth_,
        uint256 virtualToken_,
        uint256 graduationEthThreshold_,
        uint256 startTime_,
        uint256 maxBuyPerWallet_
    ) {
        require(token_ != address(0) && creator_ != address(0) && platformTreasury_ != address(0), "zero");
        require(router_ != address(0), "router=0");
        require(virtualEth_ > 0 && virtualToken_ > 0, "reserves");
        require(graduationEthThreshold_ > 0, "threshold");
        token = token_;
        creator = creator_;
        platformTreasury = platformTreasury_;
        router = IUniswapV2RouterMinimal(router_);
        virtualEth = virtualEth_;
        virtualToken = virtualToken_;
        graduationEthThreshold = graduationEthThreshold_;
        startTime = startTime_;
        maxBuyPerWallet = maxBuyPerWallet_;
    }

    function ethReserve() public view returns (uint256) {
        return virtualEth + realEth;
    }

    function tokenReserve() public view returns (uint256) {
        return virtualToken - tokensSold;
    }

    function quoteBuy(uint256 ethIn) public view returns (uint256 tokensOut) {
        require(!graduated, "curve has graduated");
        require(ethIn > 0, "eth=0");
        uint256 fee = (ethIn * FEE_BPS) / 10_000;
        uint256 net = ethIn - fee;
        uint256 k = ethReserve() * tokenReserve();
        uint256 newEth = ethReserve() + net;
        uint256 newTok = k / newEth;
        require(tokenReserve() > newTok, "empty");
        tokensOut = tokenReserve() - newTok;
    }

    function quoteSell(uint256 tokenIn) public view returns (uint256 ethOut, uint256 fee, uint256 gross) {
        require(!graduated, "curve has graduated");
        require(tokenIn > 0 && tokenIn <= tokensSold, "amt");
        uint256 k = ethReserve() * tokenReserve();
        uint256 newTok = tokenReserve() + tokenIn;
        uint256 newEth = k / newTok;
        require(ethReserve() > newEth, "empty");
        gross = ethReserve() - newEth;
        if (gross > realEth) gross = realEth;
        fee = (gross * FEE_BPS) / 10_000;
        ethOut = gross - fee;
    }

    function buy(uint256 minTokensOut) external payable nonReentrant {
        _buyTo(msg.sender, minTokensOut, address(0));
    }

    function buy(uint256 minTokensOut, address referrer) external payable nonReentrant {
        _buyTo(msg.sender, minTokensOut, referrer);
    }

    function buyTo(address to, uint256 minTokensOut, address referrer) external payable nonReentrant {
        _buyTo(to, minTokensOut, referrer);
    }

    function _buyTo(address to, uint256 minTokensOut, address referrer) internal {
        require(!graduated, "curve has graduated");
        require(block.timestamp >= startTime, "not open");
        require(msg.value > 0, "eth=0");
        if (maxBuyPerWallet > 0) {
            require(boughtNative[to] + msg.value <= maxBuyPerWallet, "max buy");
        }
        uint256 tokensOut = quoteBuy(msg.value);
        require(tokensOut >= minTokensOut, "slippage: price moved against you");
        uint256 fee = (msg.value * FEE_BPS) / 10_000;
        uint256 net = msg.value - fee;
        tokensSold += tokensOut;
        realEth += net;
        boughtNative[to] += msg.value;
        bool shouldGraduate = realEth >= graduationEthThreshold;
        require(IERC20Minimal(token).transfer(to, tokensOut), "xfer");
        emit Buy(to, msg.value, tokensOut, fee, referrer);
        _splitFee(fee, referrer, to);
        if (shouldGraduate) {
            _graduate();
        }
    }

    function sell(uint256 tokenIn, uint256 minEthOut) external nonReentrant {
        require(!graduated, "curve has graduated");
        require(block.timestamp >= startTime, "not open");
        require(tokenIn > 0, "amt=0");
        (uint256 ethOut, uint256 fee, uint256 gross) = quoteSell(tokenIn);
        require(ethOut >= minEthOut, "slippage: price moved against you");
        require(IERC20Minimal(token).transferFrom(msg.sender, address(this), tokenIn), "xferFrom");
        tokensSold -= tokenIn;
        realEth -= gross;
        emit Sell(msg.sender, tokenIn, ethOut, fee);
        _splitFee(fee, address(0), msg.sender);
        (bool ok, ) = msg.sender.call{value: ethOut}("");
        require(ok, "eth out");
    }

    function _splitFee(uint256 fee, address referrer, address buyer) internal {
        if (fee == 0) return;
        uint256 plat = (fee * PLATFORM_SHARE_BPS) / 10_000;
        uint256 crea = (fee * CREATOR_SHARE_BPS) / 10_000;
        uint256 refb = fee - plat - crea;
        if (
            referrer == address(0)
            || referrer == creator
            || referrer == platformTreasury
            || referrer == buyer
        ) {
            plat += refb;
            refb = 0;
        }
        _pay(platformTreasury, plat);
        _pay(creator, crea);
        if (refb > 0) _pay(referrer, refb);
    }

    function _pay(address to, uint256 amt) internal {
        if (amt == 0) return;
        (bool ok, ) = to.call{value: amt}("");
        require(ok, "fee pay");
    }

    function _graduate() internal {
        uint256 ethSeed = realEth;
        uint256 tokSeed = IERC20Minimal(token).balanceOf(address(this));
        require(ethSeed > 0 && tokSeed > 0, "seed");
        graduated = true;
        realEth = 0;
        IERC20Minimal(token).approve(address(router), tokSeed);
        (,, uint256 lp) = router.addLiquidityETH{value: ethSeed}(
            token,
            tokSeed,
            0,
            0,
            DEAD,
            block.timestamp + 600
        );
        emit Graduated(ethSeed, tokSeed, lp);
    }

    receive() external payable {}
}
