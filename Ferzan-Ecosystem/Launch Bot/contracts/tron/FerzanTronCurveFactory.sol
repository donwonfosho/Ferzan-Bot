// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {FerzanTronCurve} from "./FerzanTronCurve.sol";
import {FerzanTronCurveToken} from "./FerzanTronCurveToken.sol";

/// @notice Launches a Ferzan Tron bonding curve: a coin (clone) + its curve (clone) in one cheap transaction.
/// No owner, no admin. Master copies, SunSwap V2 factory, WTRX, treasury, launch fee, graduation reward and
/// minimum graduation target are fixed at deploy time.
/// msg.value = launchFeeSun + optional dev buy.
contract FerzanTronCurveFactory {
    uint256 public constant MAX_START_DELAY = 7 days;

    address public immutable tokenImpl;
    address public immutable curveImpl;
    address public immutable dexFactory;
    address public immutable wtrx;
    address public immutable platformTreasury;
    uint256 public immutable launchFeeSun;
    uint256 public immutable gradRewardSun;
    uint256 public immutable minGradTarget;

    // same event signatures as the EVM factories, so the same receipt/indexer parsing works
    event CurveLaunched(address indexed curve, address indexed token, address indexed creator);
    event CurveDetails(address indexed curve, address pool, uint256 gradTarget, uint256 startTime, uint256 devBuyWei);

    constructor(
        address tokenImpl_,
        address curveImpl_,
        address dexFactory_,
        address wtrx_,
        address platformTreasury_,
        uint256 launchFeeSun_,
        uint256 gradRewardSun_,
        uint256 minGradTarget_
    ) {
        require(
            tokenImpl_ != address(0) && curveImpl_ != address(0) && dexFactory_ != address(0) && wtrx_ != address(0)
                && platformTreasury_ != address(0),
            "zero"
        );
        tokenImpl = tokenImpl_;
        curveImpl = curveImpl_;
        dexFactory = dexFactory_;
        wtrx = wtrx_;
        platformTreasury = platformTreasury_;
        launchFeeSun = launchFeeSun_;
        gradRewardSun = gradRewardSun_;
        minGradTarget = minGradTarget_;
    }

    function launch(
        string calldata name,
        string calldata symbol,
        uint256 totalSupply,
        uint256 gradTarget,
        uint256 startTime,
        uint256 maxBuyPerWallet
    ) external payable returns (address curveAddress, address tokenAddress) {
        require(msg.value >= launchFeeSun, "launch fee");
        require(bytes(name).length > 0 && bytes(name).length <= 64, "name");
        require(bytes(symbol).length > 0 && bytes(symbol).length <= 16, "symbol");
        require(gradTarget >= minGradTarget, "grad target too low");
        uint256 start = startTime < block.timestamp ? block.timestamp : startTime;
        require(start <= block.timestamp + MAX_START_DELAY, "start too late");

        tokenAddress = _clone(tokenImpl);
        curveAddress = _clone(curveImpl);
        FerzanTronCurveToken(tokenAddress).initialize(name, symbol, totalSupply, curveAddress);
        FerzanTronCurve(curveAddress).initialize(
            FerzanTronCurve.Init({
                token: tokenAddress,
                creator: msg.sender,
                platformTreasury: platformTreasury,
                wtrx: wtrx,
                dexFactory: dexFactory,
                curveSupply: totalSupply,
                gradTarget: gradTarget,
                startTime: start,
                maxBuyPerWallet: maxBuyPerWallet,
                gradReward: gradRewardSun
            })
        );
        if (launchFeeSun > 0) {
            (bool ok,) = platformTreasury.call{value: launchFeeSun}("");
            require(ok, "fee transfer");
        }
        uint256 devBuy = msg.value - launchFeeSun;
        if (devBuy > 0) FerzanTronCurve(curveAddress).devBuy{value: devBuy}(msg.sender);
        emit CurveLaunched(curveAddress, tokenAddress, msg.sender);
        emit CurveDetails(curveAddress, address(0), gradTarget, start, devBuy); // pool is created at graduation
    }

    function _clone(address impl) internal returns (address instance) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x3d602d80600a3d3981f3363d3d373d3d3d363d73000000000000000000000000)
            mstore(add(ptr, 0x14), shl(0x60, impl))
            mstore(add(ptr, 0x28), 0x5af43d82803e903d91602b57fd5bf30000000000000000000000000000000000)
            instance := create(0, ptr, 0x37)
        }
        require(instance != address(0), "clone");
    }
}
