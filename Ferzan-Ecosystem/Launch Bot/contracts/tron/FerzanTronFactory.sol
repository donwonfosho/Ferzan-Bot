// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {FerzanTrc20} from "./FerzanTrc20.sol";

/// @notice Ferzan plain-launch factory for Tron. No owner, no admin, nothing to upgrade.
/// launchToken() clones the master FerzanTrc20, gives the whole fixed supply to the caller and
/// forwards the launch fee (TRX, in sun) to the treasury. Treasury, fee and master are fixed forever.
contract FerzanTronFactory {
    address public immutable implementation;
    address public immutable platformTreasury;
    uint256 public immutable launchFeeSun;

    event TokenLaunched(address indexed token, address indexed creator, string name, string symbol, uint256 supply);

    constructor(address implementation_, address platformTreasury_, uint256 launchFeeSun_) {
        require(implementation_ != address(0) && platformTreasury_ != address(0), "zero");
        implementation = implementation_;
        platformTreasury = platformTreasury_;
        launchFeeSun = launchFeeSun_;
    }

    function launchToken(string calldata name_, string calldata symbol_, uint256 supply) external payable returns (address token) {
        require(msg.value == launchFeeSun, "launch fee");
        require(bytes(name_).length > 0 && bytes(name_).length <= 64, "name");
        require(bytes(symbol_).length > 0 && bytes(symbol_).length <= 16, "symbol");
        require(supply > 0 && supply <= 1e30, "supply");
        token = _clone(implementation);
        FerzanTrc20(token).initialize(name_, symbol_, supply, msg.sender);
        if (msg.value > 0) {
            (bool ok,) = platformTreasury.call{value: msg.value}("");
            require(ok, "fee transfer");
        }
        emit TokenLaunched(token, msg.sender, name_, symbol_, supply);
    }

    /// EIP-1167 minimal proxy: 45 bytes of code that delegate every call to the master copy.
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
