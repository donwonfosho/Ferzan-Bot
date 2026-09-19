// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {LaunchToken} from "./LaunchToken.sol";
import {BondingCurve} from "./BondingCurve.sol";
import {IERC20Minimal} from "./IUniswapV2RouterMinimal.sol";

contract BondingCurveFactory {
    address public immutable router;
    address public immutable platformTreasury;

    event CurveLaunched(address indexed curve, address indexed token, address indexed creator);

    constructor(address router_, address platformTreasury_) {
        require(router_ != address(0) && platformTreasury_ != address(0), "zero");
        router = router_;
        platformTreasury = platformTreasury_;
    }

    function launch(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        uint256 graduationEthThreshold_,
        uint256 virtualEthReserve_,
        uint256 virtualTokenReserve_
    ) external payable returns (address curveAddress, address tokenAddress) {
        return launchFull(
            name_,
            symbol_,
            totalSupply_,
            graduationEthThreshold_,
            virtualEthReserve_,
            virtualTokenReserve_,
            0,
            0,
            new address[](0),
            new uint256[](0)
        );
    }

    function launchFull(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        uint256 graduationEthThreshold_,
        uint256 virtualEthReserve_,
        uint256 virtualTokenReserve_,
        uint256 startTime_,
        uint256 maxBuyPerWallet_,
        address[] memory allocWallets,
        uint256[] memory allocBps
    ) public payable returns (address curveAddress, address tokenAddress) {
        require(allocWallets.length == allocBps.length, "alloc len");
        LaunchToken token = new LaunchToken(name_, symbol_, totalSupply_, address(this), "");
        BondingCurve curve = new BondingCurve(
            address(token),
            msg.sender,
            platformTreasury,
            router,
            virtualEthReserve_,
            virtualTokenReserve_,
            graduationEthThreshold_,
            startTime_,
            maxBuyPerWallet_
        );
        uint256 seeded = totalSupply_;
        uint256 i;
        for (i = 0; i < allocWallets.length; i++) {
            require(allocBps[i] <= 2000, "alloc cap 20%");
            uint256 cut = (totalSupply_ * allocBps[i]) / 10_000;
            if (cut > 0 && allocWallets[i] != address(0)) {
                require(IERC20Minimal(address(token)).transfer(allocWallets[i], cut), "alloc");
                seeded -= cut;
            }
        }
        require(IERC20Minimal(address(token)).transfer(address(curve), seeded), "seed tokens");
        if (msg.value > 0) {
            curve.buyTo{value: msg.value}(msg.sender, 0, address(0));
        }
        emit CurveLaunched(address(curve), address(token), msg.sender);
        return (address(curve), address(token));
    }
}
