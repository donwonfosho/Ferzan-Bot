// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface ICurveBuy {
    function buy(uint256 minTokensOut) external payable;
}

/// Test attacker: on receiving the platform fee, tries to reenter buy().
contract ReenteringTreasury {
    address public curve;
    bool public armed;

    function setCurve(address curve_) external {
        curve = curve_;
        armed = true;
    }

    receive() external payable {
        // Ignore funding. Only reenter when the curve pays the fee mid-buy.
        if (!armed || curve == address(0) || msg.sender != curve) return;
        armed = false;
        ICurveBuy(curve).buy{value: 1}(0);
    }
}
