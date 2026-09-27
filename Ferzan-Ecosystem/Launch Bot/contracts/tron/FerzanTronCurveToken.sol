// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface ICurveState {
    function graduated() external view returns (bool);
}

/// @notice TRC-20 for Ferzan Tron bonding-curve launches. Fixed supply, no owner, no mint, 6 decimals.
/// Every launch is an EIP-1167 clone of one master copy, initialized once by the factory in the launch
/// transaction; the whole supply goes to the curve.
///
/// Until the curve graduates:
///   - nobody but the curve can send this coin to its SunSwap pool (the pool address is known in advance),
///     so nobody can open the pool early at a fake price;
///   - the coin can only move between wallets and the curve, not into other contracts (defense in depth).
/// Both rules switch off by themselves the moment the curve graduates.
contract FerzanTronCurveToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public curve;
    address public pool;
    bool public open; // set once the curve has graduated, so the lock stops costing energy
    bool private _initialized;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor() {
        _initialized = true; // lock the master copy
    }

    function initialize(string calldata name_, string calldata symbol_, uint256 supply, address curve_, address pool_)
        external
    {
        require(!_initialized, "initialized");
        require(curve_ != address(0) && pool_ != address(0) && supply > 0, "args");
        _initialized = true;
        name = name_;
        symbol = symbol_;
        totalSupply = supply;
        curve = curve_;
        pool = pool_;
        balanceOf[curve_] = supply;
        emit Transfer(address(0), curve_, supply);
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
        if (!open) {
            address c = curve;
            if (ICurveState(c).graduated()) {
                open = true;
            } else if (from != c && to != c) {
                require(to != pool, "pool opens at graduation");
                require(to.code.length == 0, "contracts can receive this coin after graduation");
            }
        }
        uint256 b = balanceOf[from];
        require(b >= value, "balance");
        unchecked {
            balanceOf[from] = b - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
