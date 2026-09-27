// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Ferzan TRC-20 (Tron). Fixed supply, no owner, no mint, no pause, 6 decimals.
/// Deployed once as the master copy; every launch is a tiny EIP-1167 clone of it that is
/// initialized exactly once by the factory in the same transaction (so launches stay cheap
/// in energy). The master copy itself can never be initialized.
contract FerzanTrc20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool private _initialized;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor() {
        _initialized = true; // lock the master copy
    }

    function initialize(string calldata name_, string calldata symbol_, uint256 supply, address to) external {
        require(!_initialized, "initialized");
        require(to != address(0), "to");
        _initialized = true;
        name = name_;
        symbol = symbol_;
        totalSupply = supply;
        balanceOf[to] = supply;
        emit Transfer(address(0), to, supply);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _move(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 a = allowance[from][msg.sender];
        if (a != type(uint256).max) {
            require(a >= value, "allowance");
            allowance[from][msg.sender] = a - value;
        }
        _move(from, to, value);
        return true;
    }

    function _move(address from, address to, uint256 value) internal {
        require(to != address(0), "to");
        uint256 b = balanceOf[from];
        require(b >= value, "balance");
        unchecked {
            balanceOf[from] = b - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
