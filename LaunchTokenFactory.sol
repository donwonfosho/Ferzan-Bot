// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {LaunchToken} from "./LaunchToken.sol";
import {IERC20Minimal} from "./IUniswapV2RouterMinimal.sol";

contract LaunchTokenFactory {
    address public immutable platformTreasury;
    uint256 public immutable launchFeeWei;

    event TokenLaunched(address indexed token, address indexed creator, string name, string symbol, uint256 supply);
    event LaunchFeePaid(address indexed creator, uint256 amount);

    constructor(address platformTreasury_, uint256 launchFeeWei_) {
        require(platformTreasury_ != address(0), "treasury=0");
        platformTreasury = platformTreasury_;
        launchFeeWei = launchFeeWei_;
    }

    function launchToken(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_
    ) external payable returns (address) {
        _takeFee();
        LaunchToken token = new LaunchToken(name_, symbol_, totalSupply_, msg.sender, projectUrl_);
        emit TokenLaunched(address(token), msg.sender, name_, symbol_, totalSupply_);
        return address(token);
    }

    function launchTokenWithAlloc(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_,
        address[] memory wallets,
        uint256[] memory bps
    ) external payable returns (address) {
        require(wallets.length == bps.length, "alloc len");
        _takeFee();
        LaunchToken token = new LaunchToken(name_, symbol_, totalSupply_, address(this), projectUrl_);
        uint256 left = totalSupply_;
        for (uint256 i = 0; i < wallets.length; i++) {
            require(bps[i] <= 2000, "alloc cap 20%");
            uint256 cut = (totalSupply_ * bps[i]) / 10_000;
            if (cut > 0 && wallets[i] != address(0)) {
                require(IERC20Minimal(address(token)).transfer(wallets[i], cut), "alloc");
                left -= cut;
            }
        }
        require(IERC20Minimal(address(token)).transfer(msg.sender, left), "creator");
        emit TokenLaunched(address(token), msg.sender, name_, symbol_, totalSupply_);
        return address(token);
    }

    function _takeFee() internal {
        require(msg.value >= launchFeeWei, "launch fee");
        if (msg.value > 0) {
            (bool ok, ) = platformTreasury.call{value: msg.value}("");
            require(ok, "fee xfer");
            emit LaunchFeePaid(msg.sender, msg.value);
        }
    }
}
