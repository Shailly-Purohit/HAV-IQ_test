trigger AccountTestTrigger on Account (before insert) {
    for (Account acc : Trigger.new) {
        if (acc.Description == null) {
            acc.Description = 'Created via HAV-IQ CI/CD';
        }
    }
}
