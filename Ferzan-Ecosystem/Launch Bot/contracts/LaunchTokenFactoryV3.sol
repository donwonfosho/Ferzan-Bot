// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {LaunchToken} from "./LaunchToken.sol";
import {IERC20Minimal} from "./IUniswapV2RouterMinimal.sol";

/// @notice v3 of the plain-launch factory: same launches as v1, plus *WithSalt variants that
/// deploy the token via CREATE2 at a vanity address (salt bound to msg.sender).
contract LaunchTokenFactoryV3 {
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
        return _plain(name_, symbol_, totalSupply_, projectUrl_, bytes32(0));
    }

    function launchTokenWithSalt(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_,
        bytes32 salt
    ) external payable returns (address) {
        require(salt != bytes32(0), "salt");
        return _plain(name_, symbol_, totalSupply_, projectUrl_, salt);
    }

    function _plain(string memory name_, string memory symbol_, uint256 totalSupply_, string memory projectUrl_, bytes32 salt)
        internal
        returns (address)
    {
        _takeFee();
        LaunchToken token = _deploy(name_, symbol_, totalSupply_, msg.sender, projectUrl_, salt);
        emit TokenLaunched(address(token), msg.sender, name_, symbol_, totalSupply_);
        return address(token);
    }

    function _deploy(string memory n, string memory s, uint256 supply, address owner, string memory url, bytes32 salt)
        internal
        returns (LaunchToken)
    {
        if (salt == bytes32(0)) return new LaunchToken(n, s, supply, owner, url);
        return new LaunchToken{salt: keccak256(abi.encode(msg.sender, salt))}(n, s, supply, owner, url);
    }

    /// keccak256 of the token init code. `withAlloc` = the token is minted to this factory first.
    function tokenInitCodeHash(string memory n, string memory s, uint256 supply, address creator, string memory url, bool withAlloc)
        public
        view
        returns (bytes32)
    {
        address owner = withAlloc ? address(this) : creator;
        return keccak256(abi.encodePacked(type(LaunchToken).creationCode, abi.encode(n, s, supply, owner, url)));
    }

    function predictToken(address creator, bytes32 salt, string memory n, string memory s, uint256 supply, string memory url, bool withAlloc)
        external
        view
        returns (address)
    {
        bytes32 h = keccak256(
            abi.encodePacked(bytes1(0xff), address(this), keccak256(abi.encode(creator, salt)),
                tokenInitCodeHash(n, s, supply, creator, url, withAlloc))
        );
        return address(uint160(uint256(h)));
    }

    function launchTokenWithAlloc(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_,
        address[] memory wallets,
        uint256[] memory bps
    ) external payable returns (address) {
        return _alloc(name_, symbol_, totalSupply_, projectUrl_, wallets, bps, bytes32(0));
    }

    function launchTokenWithAllocAndSalt(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_,
        address[] memory wallets,
        uint256[] memory bps,
        bytes32 salt
    ) external payable returns (address) {
        require(salt != bytes32(0), "salt");
        return _alloc(name_, symbol_, totalSupply_, projectUrl_, wallets, bps, salt);
    }

    function _alloc(
        string memory name_,
        string memory symbol_,
        uint256 totalSupply_,
        string memory projectUrl_,
        address[] memory wallets,
        uint256[] memory bps,
        bytes32 salt
    ) internal returns (address) {
        require(wallets.length == bps.length, "alloc len");
        _takeFee();
        LaunchToken token = _deploy(name_, symbol_, totalSupply_, address(this), projectUrl_, salt);
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
