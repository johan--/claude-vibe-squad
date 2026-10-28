// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// PLANTED BUG: classic single-function reentrancy. `withdraw` sends ether with a
/// low-level call before zeroing the caller's balance, so a malicious receiver can
/// re-enter and withdraw the same balance repeatedly until the bank is empty.
///
/// class:          reentrancy (CEI violation) / SWC-107
/// expected tool:  slither  -> detector `reentrancy-eth` on ReentrantBank.withdraw
///                 semgrep  -> rule `state-write-after-external-call`
///                 forge    -> ReentrancyExploit.t.sol drains the bank
/// repaired form:  negative-control/ReentrantBank.sol (state written before the call)
contract ReentrantBank {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 bal = balances[msg.sender];
        require(bal > 0, "no balance");
        (bool ok, ) = msg.sender.call{value: bal}("");
        require(ok, "send failed");
        balances[msg.sender] = 0;
    }
}
