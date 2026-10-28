// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// NEGATIVE CONTROL: the repaired form of src/ReentrantBank.sol. Checks-Effects-
/// Interactions is respected, so slither, semgrep and the forge exploit must all
/// go quiet here. If they do not, the bench cannot fail and proves nothing.
contract ReentrantBank {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 bal = balances[msg.sender];
        require(bal > 0, "no balance");
        balances[msg.sender] = 0;
        (bool ok, ) = msg.sender.call{value: bal}("");
        require(ok, "send failed");
    }
}
