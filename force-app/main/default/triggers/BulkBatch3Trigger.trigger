trigger BulkBatch3Trigger on Bulk_Batch_3_Record__c (before insert) {
    for (Bulk_Batch_3_Record__c record : Trigger.new) {
        if (record.Name == null) {
            record.Name = 'Batch 3 Test Record';
        }
    }
}