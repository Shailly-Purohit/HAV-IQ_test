public with sharing class HAVIQAccountHealthCheck {
    public static String evaluateAccountStatus(String rating, Decimal annualRevenue) {
        if (rating == 'Hot' && annualRevenue != null && annualRevenue >= 1000000) {
            return 'Enterprise Priority';
        }
        if (annualRevenue != null && annualRevenue > 0) {
            return 'Active Pipeline';
        }
        return 'Needs Qualification';
    }
}