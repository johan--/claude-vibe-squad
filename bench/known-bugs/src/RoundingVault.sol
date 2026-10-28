// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// PLANTED BUG: share accounting rounds in the withdrawer's favour. `withdraw`
/// converts assets to shares with floor division, so once the share price exceeds
/// 1 (any accrued yield) a withdrawal burns FEWER shares than it is worth. Repeated
/// small withdrawals extract assets while burning zero shares, diluting everyone else.
/// This is the Balancer / Bunni / zkLend class: no unsafe call, no missing modifier,
/// just a division that rounds the wrong way.
///
/// class:          rounding / precision loss favouring the caller
/// expected tool:  forge -> RoundingExploit.t.sol (PoC + fuzz). Deliberately NOT a
///                 static-analysis expectation: slither and semgrep do not decide
///                 which way a division should round, and this contract is here to
///                 prove the dynamic half of the pipeline is doing real work.
/// repaired form:  negative-control/RoundingVault.sol (round-up on the burn path)
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

    /// Yield arriving from strategies: raises the share price without minting shares.
    function accrueYield() external payable {
        require(totalShares > 0, "no shares");
        totalAssets += msg.value;
    }

    function withdraw(uint256 assets) external {
        require(assets > 0 && assets <= totalAssets, "bad amount");
        uint256 burned = (assets * totalShares) / totalAssets;
        shares[msg.sender] -= burned;
        totalShares -= burned;
        totalAssets -= assets;
        (bool ok, ) = msg.sender.call{value: assets}("");
        require(ok, "send failed");
    }
}
