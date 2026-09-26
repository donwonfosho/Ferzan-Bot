// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Fixed-supply ERC-20 for Ferzan bonding-curve launches.
/// - The whole supply is minted once, to the launch factory. No mint function exists.
/// - No owner / admin. The only privileged action is the factory's one-time `setup`
///   call made inside the same launch transaction.
/// - Until the curve graduates, nobody except the curve can send tokens to the DEX pool.
///   That stops anyone from creating / seeding the pool early at a fake price, which would
///   otherwise break graduation. The lock lifts itself the moment the curve graduates.
/// - Team-wallet allocations are locked until graduation (they can't be moved or sold into
///   the curve), so a team wallet can never drain what buyers paid into the curve.
contract CurveToken is ERC20 {
    address public immutable factory;
    address public curve;
    address public pool;
    bool public poolOpen;
    mapping(address => uint256) public lockedUntilGraduation;

    constructor(string memory name_, string memory symbol_, uint256 totalSupply_) ERC20(name_, symbol_) {
        require(totalSupply_ > 0, "supply=0");
        factory = msg.sender;
        _mint(msg.sender, totalSupply_);
    }

    /// One-time, factory-only, called in the launch transaction: sets the curve + pool and
    /// sends the team allocations (locked until graduation).
    function setup(address curve_, address pool_, address[] calldata wallets, uint256[] calldata amounts)
        external
    {
        require(msg.sender == factory, "only factory");
        require(curve == address(0), "already set");
        require(curve_ != address(0) && pool_ != address(0), "zero");
        require(wallets.length == amounts.length, "len");
        curve = curve_;
        pool = pool_;
        for (uint256 i = 0; i < wallets.length; i++) {
            _transfer(msg.sender, wallets[i], amounts[i]);
            lockedUntilGraduation[wallets[i]] += amounts[i];
        }
    }

    function poolUnlocked() public view returns (bool) {
        address c = curve;
        return c != address(0) && IGraduated(c).graduated();
    }

    function _update(address from, address to, uint256 value) internal override {
        if (!poolOpen && to == pool && to != address(0) && from != curve) {
            if (!poolUnlocked()) revert("pool locked until graduation");
            poolOpen = true;
        }
        uint256 locked = lockedUntilGraduation[from];
        if (locked != 0 && from != address(0)) {
            if (poolUnlocked()) {
                delete lockedUntilGraduation[from];
            } else {
                require(balanceOf(from) >= value + locked, "team tokens locked until graduation");
            }
        }
        super._update(from, to, value);
    }
}

interface IGraduated {
    function graduated() external view returns (bool);
}
