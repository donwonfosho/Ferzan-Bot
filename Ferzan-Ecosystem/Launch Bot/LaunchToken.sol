// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Fixed-supply ERC-20. No mint function exists after construction.
contract LaunchToken is ERC20, Ownable {
    string public projectUrl;
    uint8 private immutable _tokenDecimals;

    constructor(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        address initialOwner_,
        string memory projectUrl_
    ) ERC20(name_, symbol_) Ownable(initialOwner_) {
        require(initialOwner_ != address(0), "owner=0");
        require(totalSupply_ > 0, "supply=0");
        projectUrl = projectUrl_;
        _tokenDecimals = 18;
        _mint(initialOwner_, totalSupply_);
    }

    function decimals() public view override returns (uint8) {
        return _tokenDecimals;
    }
}
