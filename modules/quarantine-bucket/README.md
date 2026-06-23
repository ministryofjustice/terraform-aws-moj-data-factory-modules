# Data Factory Quarantine Bucket

Provisions a secure, isolated Amazon S3 bucket for storing items that fail a malware/virus scan in a Justice Data Factory data-processing pipeline. The bucket is a deliberate "dead end": data lands in a separate landing bucket, is scanned (e.g. with Amazon GuardDuty Malware Protection for S3 or ClamAV via a Lambda), and any object that fails the scan is moved here.

The module wraps [`terraform-aws-modules/s3-bucket/aws`](https://github.com/terraform-aws-modules/terraform-aws-s3-bucket) and adds the controls needed to keep suspected malware contained:

- **Deny-by-default bucket policy** — the supplied writer (scan) role ARNs are granted write access, the supplied reader (incident-response) role ARNs are granted read access, and all other principals are denied. The bucket-owning account root is always trusted to avoid lockout.
- **Block Public Access** fully enabled, and ACLs disabled (`BucketOwnerEnforced`).
- **Dedicated customer-managed KMS key** with a restrictive key policy, so even an over-broad S3 grant cannot decrypt objects without separate KMS access. S3 Bucket Keys are enabled to reduce KMS request costs.
- **Encryption enforced** — SSE-KMS by default with the module's customer-managed KMS key, plus policies denying insecure (non-TLS) transport.
- **Versioning + S3 Object Lock (GOVERNANCE mode)** for tamper-resistant evidence retention.
- **Lifecycle expiry** so suspected malware is not hoarded indefinitely, plus cleanup of incomplete multipart uploads.
- **SNS notification** on object creation so the security team is alerted the moment something is quarantined.


### Out of scope

The scan Lambda and its role creation, source-side IAM permissions, the landing/processing buckets, GuardDuty/ClamAV configuration, CloudTrail S3 data events, account-level Public Access Block, and any Slack/Lambda notification delivery are intentionally **not** managed by this module. Consumers wire their own subscribers to the exported `sns_topic_arn`.

## Usage

```hcl
module "quarantine_bucket" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/quarantine-bucket?ref=<git hash>"

  environment = local.environment
  name_prefix = "moj-data-factory"

  writer_role_arns = [aws_iam_role.scan_lambda.arn]
  reader_role_arns = [aws_iam_role.incident_response.arn]

  object_lock_retention_days = 90
  expiry_days                = 90

}
```

## Notes

- **Object Lock and lifecycle interplay:** with GOVERNANCE retention, the lifecycle rule cannot permanently delete object versions until their retention period expires. Keep `expiry_days` greater than or equal to `object_lock_retention_days` to avoid surprises.
- **Default lockdown:** if `writer_role_arns` and `reader_role_arns` are both empty, only the account root can access the bucket. Supply the scan and incident-response role ARNs for normal operation.
- **Writer and reader access:** role ARNs passed to `writer_role_arns` and `reader_role_arns` are granted destination access to this bucket and KMS key. Callers still need to grant source bucket and source KMS permissions when copying from a landing bucket.
- **Upload encryption:** writers can rely on the bucket default SSE-KMS configuration; upload requests do not need to pass explicit `ServerSideEncryption` or `SSEKMSKeyId` values. If callers do pass them, use the exported `kms_key_arn`.
- `force_destroy` is fixed to `false` to protect quarantined evidence.

<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
<!-- markdownlint-enable -->
<!-- prettier-ignore-end -->
