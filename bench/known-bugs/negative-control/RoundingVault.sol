// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// NEGATIVE CONTROL: the repaired form of src/RoundingVault.sol. The burn path
/// rounds UP, so a withdrawal never costs the caller fewer shares than it is worth
/// and the forge exploit must stop reproducing.
contract RoundingVault {
    mapping(address => uint256) public shares;
    uint256 public totalShares;
    uint256 public totalAssets;

    function deposit() external payable {
        require(msg.value > 0, "zero deposit");
        uint256 minted = totalShares == 0 ? msg.value : (msg.value * totalShares) / totalAssets;
        require(minted > 0, "zero shares");
        shares[msg.sender] += minted;
        totalShares += minted;
        totalAssets += msg.value;
    }

    function accrueYield() external payable {
        require(totalShares > 0, "no shares");
        totalAssets += msg.value;
    }

    function withdraw(uint256 assets) external {
        require(assets > 0 && assets <= totalAssets, "bad amount");
        uint256 burned = (assets * totalShares + totalAssets - 1) / totalAssets;
        shares[msg.sender] -= burned;
        totalShares -= burned;
        totalAssets -= assets;
        (bool ok, ) = msg.sender.call{value: assets}("");
        require(ok, "send failed");
    }
}
