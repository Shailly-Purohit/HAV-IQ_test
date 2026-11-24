
@isTest
private class SimpleContractTriggerTest {
    @isTest
    static void testSimpleContractTrigger() {
        Contract testContract = new Contract(
            Status = 'Draft',
            StartDate = Date.today(),
            ContractTerm = 12
        );

        Test.startTest();
        insert testContract;
        Test.stopTest();

        System.assertNotEquals(null, testContract.Id);
    }
}
