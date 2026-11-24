
trigger SimpleContractTrigger on Contract (before insert) {
    System.debug('Simple Contract Trigger executed.');
}
