// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20Minimal} from "../IUniswapV2RouterMinimal.sol";

contract MockWETH {
    string public name = "Wrapped Ether";
    string public symbol = "WETH";
    mapping(address => uint256) public balanceOf;
    event Deposit(address indexed dst, uint256 wad);
    receive() external payable {
        balanceOf[msg.sender] += msg.value;
        emit Deposit(msg.sender, msg.value);
    }
}

contract MockLP {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;
    function mint(address to, uint256 amt) external {
        balanceOf[to] += amt;
        totalSupply += amt;
    }
}

/// Minimal router used only in Hardhat tests.
contract MockUniswapV2Router {
    MockWETH public wethToken;
    MockLP public lp;

    constructor() {
        wethToken = new MockWETH();
        lp = new MockLP();
    }

    function WETH() external view returns (address) {
        return address(wethToken);
    }

    function addLiquidityETH(
        address token,
        uint256 amountTokenDesired,
        uint256,
        uint256,
        address to,
        uint256
    ) external payable returns (uint256 amountToken, uint256 amountETH, uint256 liquidity) {
        amountToken = amountTokenDesired;
        amountETH = msg.value;
        require(IERC20Minimal(token).transferFrom(msg.sender, address(this), amountToken), "tok");
        liquidity = amountETH + 1;
        lp.mint(to, liquidity);
    }
}
