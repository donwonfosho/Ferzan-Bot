// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {CurveToken} from "./CurveToken.sol";
import {FerzanCurve} from "./FerzanCurve.sol";

interface IV2Factory {
    function getPair(address a, address b) external view returns (address);
    function createPair(address a, address b) external returns (address);
}

/// @notice v3: launches a CurveToken + FerzanCurve in one transaction, optionally at a
/// vanity token address (CREATE2; the salt is bound to msg.sender so nobody can take it).
/// No owner / admin. DEX, WETH, treasury and launch fee are fixed at deploy time.
/// msg.value = launchFeeWei + optional dev buy.
contract FerzanCurveFactoryV3 {
    uint256 public constant MAX_ALLOC_BPS_TOTAL = 2000; // team wallets: 20% of supply max
    uint256 public constant MAX_START_DELAY = 7 days;

    address public immutable dexFactory;
    address public immutable weth;
    address public immutable platformTreasury;
    uint256 public immutable launchFeeWei;

    // same signature as the v1 factory, so existing receipt parsing keeps working
    event CurveLaunched(address indexed curve, address indexed token, address indexed creator);
    event CurveDetails(address indexed curve, address pool, uint256 gradTarget, uint256 startTime, uint256 devBuyWei);

    constructor(address dexFactory_, address weth_, address platformTreasury_, uint256 launchFeeWei_) {
        require(dexFactory_ != address(0) && weth_ != address(0) && platformTreasury_ != address(0), "zero");
        dexFactory = dexFactory_;
        weth = weth_;
        platformTreasury = platformTreasury_;
        launchFeeWei = launchFeeWei_;
    }

    struct LaunchParams {
        string name;
        string symbol;
        uint256 totalSupply;
        uint256 gradTarget;
        uint256 startTime;
        uint256 maxBuyPerWallet;
        address[] allocWallets;
        uint256[] allocBps;
    }

    /// p = (name, symbol, totalSupply, gradTarget, startTime, maxBuyPerWallet, allocWallets, allocBps)
    function launch(LaunchParams calldata p)
        external
        payable
        returns (address curveAddress, address tokenAddress)
    {
        require(msg.value >= launchFeeWei, "launch fee");
        _checkParams(p);
        return _launch(p, new CurveToken(p.name, p.symbol, p.totalSupply));
    }

    /// Same as launch(), with the token deployed via CREATE2 using keccak256(abi.encode(msg.sender, salt)).
    function launchWithSalt(LaunchParams calldata p, bytes32 salt)
        external
        payable
        returns (address curveAddress, address tokenAddress)
    {
        require(msg.value >= launchFeeWei, "launch fee");
        require(salt != bytes32(0), "salt");
        _checkParams(p);
        return _launch(p, new CurveToken{salt: keccak256(abi.encode(msg.sender, salt))}(p.name, p.symbol, p.totalSupply));
    }

    /// keccak256 of the token's init code for these constructor args (for off-chain address search).
    function tokenInitCodeHash(string calldata name, string calldata symbol, uint256 totalSupply)
        public
        pure
        returns (bytes32)
    {
        return keccak256(abi.encodePacked(type(CurveToken).creationCode, abi.encode(name, symbol, totalSupply)));
    }

    function predictToken(address creator, bytes32 salt, string calldata name, string calldata symbol, uint256 totalSupply)
        external
        view
        returns (address)
    {
        bytes32 h = keccak256(
            abi.encodePacked(bytes1(0xff), address(this), keccak256(abi.encode(creator, salt)),
                tokenInitCodeHash(name, symbol, totalSupply))
        );
        return address(uint160(uint256(h)));
    }

    function _checkParams(LaunchParams calldata p) internal pure {
        require(p.allocWallets.length == p.allocBps.length && p.allocWallets.length <= 10, "allocs");
        require(bytes(p.name).length > 0 && bytes(p.name).length <= 64, "name");
        require(bytes(p.symbol).length > 0 && bytes(p.symbol).length <= 16, "symbol");
    }

    function _launch(LaunchParams memory p, CurveToken token) internal returns (address, address) {
        uint256 start = p.startTime < block.timestamp ? block.timestamp : p.startTime;
        require(start <= block.timestamp + MAX_START_DELAY, "start too late");
        address pool = IV2Factory(dexFactory).getPair(address(token), weth);
        if (pool == address(0)) pool = IV2Factory(dexFactory).createPair(address(token), weth);

        uint256 curveSupply = p.totalSupply - _allocTotal(p);
        FerzanCurve curve = new FerzanCurve(
            address(token), msg.sender, platformTreasury, weth, pool, curveSupply, p.gradTarget, start, p.maxBuyPerWallet
        );
        uint256[] memory amounts = new uint256[](p.allocWallets.length);
        for (uint256 i = 0; i < p.allocWallets.length; i++) {
            amounts[i] = (p.totalSupply * p.allocBps[i]) / 10_000;
        }
        token.setup(address(curve), pool, p.allocWallets, amounts); // team tokens locked until graduation
        require(token.transfer(address(curve), curveSupply), "seed");

        if (launchFeeWei > 0) {
            (bool ok,) = platformTreasury.call{value: launchFeeWei}("");
            require(ok, "fee transfer");
        }
        uint256 devBuyWei = msg.value - launchFeeWei;
        if (devBuyWei > 0) {
            curve.devBuy{value: devBuyWei}(msg.sender);
        }
        emit CurveLaunched(address(curve), address(token), msg.sender);
        emit CurveDetails(address(curve), pool, p.gradTarget, start, devBuyWei);
        return (address(curve), address(token));
    }

    function _allocTotal(LaunchParams memory p) internal pure returns (uint256 total) {
        uint256 bpsTotal;
        for (uint256 i = 0; i < p.allocWallets.length; i++) {
            require(p.allocWallets[i] != address(0) && p.allocBps[i] > 0, "alloc");
            bpsTotal += p.allocBps[i];
            total += (p.totalSupply * p.allocBps[i]) / 10_000;
        }
        require(bpsTotal <= MAX_ALLOC_BPS_TOTAL, "team wallets max 20%");
    }
}
